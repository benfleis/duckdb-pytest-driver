"""How resource outputs become an engine's preamble: one renderer per (dialect, action).

    duckdb     SQL sent to a DuckDB shell over its stdin: LOAD, SET, CREATE SECRET ... SCOPE
    unittest   the init file of a `.test` batch: `require`, SET, CREATE SECRET ... SCOPE; a secret
               value is a `{NAME}` the binary fills from a variable passed through to it, never
               the value in a file
    spark      runtime configuration of a Spark session; a secret is a per-bucket s3a setting

The actions are `requires` (a list of extensions), `settings` (name -> value) and `secret` (a
Credential and the Storages it is scoped to, among a Test's inputs).
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
    requires: list = field(default_factory=list)  # extensions the statements need loaded first

    def __add__(self, other):
        return Preamble(
            self.sql + other.sql,
            {**self.conf, **other.conf},
            self.passthrough + other.passthrough,
            self.requires + [r for r in other.requires if r not in self.requires],
        )


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
    """`definition`'s requires, settings, a secret for each credential input matched to the
    storages among `inputs` it is scoped to, then its init statements."""
    out = Preamble()
    if definition.requires:
        out += render(dialect, "requires", list(definition.requires))
    if definition.settings:
        out += render(dialect, "settings", dict(definition.settings))
    for credential, storages in pairs(inputs):
        out += render(dialect, "secret", storages, credential)
    return out + Preamble(sql=list(definition.init))


def pairs(inputs):
    """(credential, [storage, ...]) for each credential among `inputs` matched to the storages among
    `inputs` it is scoped to, in the credential's declared scope order. A credential scoped to
    nothing among `inputs` is left out."""
    inputs = list(inputs)
    storages = {r.id: r for r in inputs if r.definition.kind == "storage"}
    out = []
    for c in inputs:
        if c.definition.kind != "credential":
            continue
        matched = [storages[s] for s in c.definition.scope_storages if s in storages]
        if matched:
            out.append((c, matched))
    return out


def quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _secret_name(storages):
    return "ducktest_" + "_".join(re.sub(r"\W", "_", s.id) for s in storages)


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


def _storage_options(storage, value_of):
    """The type and connection options of `storage`'s secret, without its scope; None for a storage
    that needs no secret (a local path)."""
    facts = _facts(storage)
    if facts["scheme"] == "file":
        return None
    if facts["scheme"] == "s3":
        options = {"TYPE": "s3"}
        options.update({opt: value_of(name) for opt, name in _S3_OPTIONS.items() if value_of(name)})
        if facts["endpoint"]:
            options["ENDPOINT"] = facts["endpoint"].split("://", 1)[-1]
            options["USE_SSL"] = facts["endpoint"].startswith("https://")
            options["URL_STYLE"] = "path"
    elif facts["scheme"] in ("az", "abfss"):
        options = {"TYPE": "azure"}
        if value_of("AZURE_STORAGE_CONNECTION_STRING"):
            options["CONNECTION_STRING"] = value_of("AZURE_STORAGE_CONNECTION_STRING")
        else:
            options["PROVIDER"] = "service_principal"
            options.update({opt: value_of(name) for opt, name in _AZURE_SPN.items() if value_of(name)})
            options["ACCOUNT_NAME"] = facts["account"]
    else:
        raise UsageError(f"storage {storage.id!r}: the duckdb dialect has no secret for {facts['scheme']}")
    return options


def _secret_options(storages, value_of):
    """The options of one secret covering `storages`, a secret value as `value_of(name)` gives it;
    None when none of them need one. SCOPE is the plain root when one storage needs a secret, a list
    of roots when several do -- both are valid DuckDB syntax, and this keeps the common, one-storage
    case unchanged."""
    needed = [(s, _storage_options(s, value_of)) for s in storages]
    needed = [(s, o) for s, o in needed if o is not None]
    if not needed:
        return None
    first, options = needed[0]
    for storage, other in needed[1:]:
        if other != options:
            raise UsageError(
                f"credential scoped to storages {[s.id for s, _ in needed]}: {first.id!r} and {storage.id!r} "
                "need different secrets, so one credential cannot cover both"
            )
    roots = [_facts(s)["root"] for s, _ in needed]
    options = dict(options, SCOPE=roots[0] if len(roots) == 1 else roots)
    return options


def _render_value(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return "[" + ", ".join(quote(v) for v in value) + "]"
    return quote(value)


def _create_secret(storages, options):
    body = ", ".join(f"{k} {_render_value(v)}" for k, v in options.items())
    return f"CREATE SECRET {_secret_name(storages)} ({body});"


@renders("duckdb", "secret")
def _duckdb_secret(storages, credential):
    options = _secret_options(storages, _values(credential).get)
    return Preamble() if options is None else Preamble(sql=[_create_secret(storages, options)])


# -----------------------------------------------------------------------------
# unittest
#


@renders("unittest", "requires")
def _unittest_requires(names):
    return Preamble(sql=[f"require {name}" for name in names])


renders("unittest", "settings")(_duckdb_settings)


_EXTENSION_OF = {"s3": "httpfs", "azure": "azure"}


@renders("unittest", "secret")
def _unittest_secret(storages, credential):
    """The same secret, with `{NAME}` for each value: the binary fills it from the variables passed
    through to it, so the init file holds no value. The binary loads no extension by itself, so the
    one of the secret type is required first."""
    names = sorted(name for name, out in credential.secrets.items() if not out.unset)
    options = _secret_options(storages, lambda name: f"{{{name}}}" if name in names else None)
    if options is None:
        return Preamble(passthrough=names)
    return Preamble(
        sql=[_create_secret(storages, options)], passthrough=names, requires=[_EXTENSION_OF[options["TYPE"]]]
    )


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
def _spark_secret(storages, credential):
    """Settings of each scoped bucket, so buckets of several stores coexist. They are set on a
    running session, where the Hadoop configuration takes them without the `spark.hadoop.` prefix
    that configures a session at boot."""
    values = _values(credential)
    conf = {}
    for storage in storages:
        facts = _facts(storage)
        if facts["scheme"] == "file":
            continue
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
        conf.update({f"fs.s3a.bucket.{bucket}.{k}": v for k, v in per_bucket.items() if v is not None})
    if conf:
        conf["fs.s3.impl"] = (
            "org.apache.hadoop.fs.s3a.S3AFileSystem"  # so a body gives Spark the `s3://` URI it gives DuckDB
        )
    return Preamble(conf=conf)
