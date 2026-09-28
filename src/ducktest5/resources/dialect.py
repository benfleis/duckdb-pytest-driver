"""How resource outputs become an engine's preamble: one renderer per (dialect, action).

    duckdb     SQL sent to a DuckDB shell over its stdin: LOAD, SET, CREATE SECRET ... SCOPE
    unittest   the init file of a `.test` batch: `require`, SET; a secret is only a name passed
               through from the process environment, never its value in a file
    spark      runtime configuration of a Spark session; a secret is a per-bucket s3a setting

The actions are `requires` (a list of extensions), `settings` (name -> value) and `secret` (a
Storage and the Credential scoped to it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..binary import UsageError

_RENDERERS = {}


@dataclass
class Preamble:
    sql: list = field(default_factory=list)  # statements, in order
    conf: dict = field(default_factory=dict)  # runtime settings of the engine
    passthrough: list = field(default_factory=list)  # process variables the engine reads secrets from

    def __add__(self, other):
        return Preamble(self.sql + other.sql, {**self.conf, **other.conf}, self.passthrough + other.passthrough)


def renders(dialect, action):
    def register(fn):
        _RENDERERS[(dialect, action)] = fn
        return fn

    return register


def render(dialect, action, *args) -> Preamble:
    try:
        renderer = _RENDERERS[(dialect, action)]
    except KeyError:
        raise UsageError(f"the {dialect} dialect cannot deliver {action}") from None
    return renderer(*args)


def preamble(dialect, definition, inputs) -> Preamble:
    """`definition`'s requires, settings, a secret for each storage input that has a credential
    scoped to it among `inputs`, then its init statements."""
    out = Preamble()
    if definition.requires:
        out += render(dialect, "requires", list(definition.requires))
    if definition.settings:
        out += render(dialect, "settings", dict(definition.settings))
    for storage, credential in pairs(inputs):
        out += render(dialect, "secret", storage, credential)
    return out + Preamble(sql=list(definition.init))


def pairs(inputs):
    """(storage, credential) for each storage among `inputs` with a credential scoped to it."""
    inputs = list(inputs)
    storages = {r.id: r for r in inputs if r.definition.kind == "storage"}
    return [
        (storages[c.definition.scope_storage], c)
        for c in inputs
        if c.definition.kind == "credential" and c.definition.scope_storage in storages
    ]


def quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _secret_name(storage):
    return "ducktest_" + re.sub(r"\W", "_", storage.id)


def _facts(storage):
    return {name: out.get() for name, out in storage.env.items()}


def _values(credential):
    return {name: out.get() for name, out in credential.secrets.items()}


# -----------------------------------------------------------------------------
# duckdb
#


@renders("duckdb", "requires")
def _duckdb_requires(names):
    return Preamble(sql=[f"LOAD {name};" for name in names])


@renders("duckdb", "settings")
def _duckdb_settings(settings):
    from ..testenv import set_statement

    return Preamble(sql=[set_statement(name, value) for name, value in settings.items()])


_S3_OPTIONS = {
    "KEY_ID": "AWS_ACCESS_KEY_ID",
    "SECRET": "AWS_SECRET_ACCESS_KEY",
    "SESSION_TOKEN": "AWS_SESSION_TOKEN",
    "REGION": "AWS_REGION",
}
_AZURE_SPN = {"TENANT_ID": "AZURE_TENANT_ID", "CLIENT_ID": "AZURE_CLIENT_ID", "CLIENT_SECRET": "AZURE_CLIENT_SECRET"}


@renders("duckdb", "secret")
def _duckdb_secret(storage, credential):
    facts, values = _facts(storage), _values(credential)
    if facts["scheme"] == "file":
        return Preamble()
    if facts["scheme"] == "s3":
        options = {"TYPE": "s3"}
        options.update({opt: values[name] for opt, name in _S3_OPTIONS.items() if values.get(name)})
        if facts["endpoint"]:
            options["ENDPOINT"] = facts["endpoint"].split("://", 1)[-1]
            options["USE_SSL"] = facts["endpoint"].startswith("https://")
            options["URL_STYLE"] = "path"
    elif facts["scheme"] in ("az", "abfss"):
        options = {"TYPE": "azure"}
        if values.get("AZURE_STORAGE_CONNECTION_STRING"):
            options["CONNECTION_STRING"] = values["AZURE_STORAGE_CONNECTION_STRING"]
        else:
            options["PROVIDER"] = "service_principal"
            options.update({opt: values[name] for opt, name in _AZURE_SPN.items() if values.get(name)})
            options["ACCOUNT_NAME"] = facts["account"]
    else:
        raise UsageError(f"storage {storage.id!r}: the duckdb dialect has no secret for {facts['scheme']}")
    options["SCOPE"] = facts["root"]
    body = ", ".join(f"{k} {str(v).lower() if isinstance(v, bool) else quote(v)}" for k, v in options.items())
    return Preamble(sql=[f"CREATE SECRET {_secret_name(storage)} ({body});"])


# -----------------------------------------------------------------------------
# unittest
#


@renders("unittest", "requires")
def _unittest_requires(names):
    return Preamble(sql=[f"require {name}" for name in names])


renders("unittest", "settings")(_duckdb_settings)


@renders("unittest", "secret")
def _unittest_secret(storage, credential):
    return Preamble(passthrough=sorted(credential.secrets))


# -----------------------------------------------------------------------------
# spark
#


@renders("spark", "requires")
def _spark_requires(names):
    raise UsageError(f"a Spark session cannot load {names} at run time; its packages are given when it boots")


@renders("spark", "settings")
def _spark_settings(settings):
    return Preamble(conf={name: str(value) for name, value in settings.items()})


@renders("spark", "secret")
def _spark_secret(storage, credential):
    """Settings of one bucket, so buckets of several stores coexist. They are set on a running
    session, where the Hadoop configuration takes them without the `spark.hadoop.` prefix that
    configures a session at boot."""
    facts, values = _facts(storage), _values(credential)
    if facts["scheme"] == "file":
        return Preamble()
    if facts["scheme"] != "s3":
        raise UsageError(f"storage {storage.id!r}: the spark dialect has no secret for {facts['scheme']}")
    bucket = facts["root"].split("://", 1)[1].split("/", 1)[0]
    per_bucket = {
        "access.key": values.get("AWS_ACCESS_KEY_ID"),
        "secret.key": values.get("AWS_SECRET_ACCESS_KEY"),
        "endpoint.region": values.get("AWS_REGION"),
        "aws.credentials.provider": "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
    }
    if facts["endpoint"]:
        per_bucket["endpoint"] = facts["endpoint"]
        per_bucket["path.style.access"] = "true"
        per_bucket["connection.ssl.enabled"] = str(facts["endpoint"].startswith("https://")).lower()
    conf = {f"fs.s3a.bucket.{bucket}.{k}": v for k, v in per_bucket.items() if v is not None}
    conf["fs.s3.impl"] = (
        "org.apache.hadoop.fs.s3a.S3AFileSystem"  # so a body gives Spark the `s3://` URI it gives DuckDB
    )
    return Preamble(conf=conf)
