"""How resource attributes become an engine's preamble: one renderer per (dialect, action), and
one per (dialect, credential class) for a credential's secret.

    duckdb     SQL sent to a DuckDB shell over its stdin: LOAD, SET, CREATE SECRET ... SCOPE
    unittest   the init file of a `.test` batch: `require`, SET, CREATE SECRET ... SCOPE
    spark      runtime configuration of a Spark session; a secret is a per-bucket s3a setting

The actions are `requires` (a list of extensions), `settings` (name -> value) and `secret` (a
Credential and the Storages it is scoped to, among a Test's inputs). The keys reach every engine
process as SDK env, never as text. An S3 secret has the AWS SDK chain read them from that env, as a
DuckDB user's does; an Azure secret names them, and takes their values from the engine's env when it
is made, since an extension that reads the keys from the secret alone (delta) needs them in it. An
Azure connection string is a scoped secret like the rest, read from the env the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..binary import UsageError
from .credential import AzureCredentialDef, S3CredentialDef

_RENDERERS = {}


@dataclass
class Preamble:
    sql: list = field(default_factory=list)  # statements, in order
    conf: dict = field(default_factory=dict)  # runtime settings of the engine
    requires: list = field(default_factory=list)  # extensions the statements need loaded first
    why: dict = field(default_factory=dict)  # extension -> what needs it, said where it is missing

    def __add__(self, other):
        return Preamble(
            self.sql + other.sql,
            {**self.conf, **other.conf},
            self.requires + [r for r in other.requires if r not in self.requires],
            {**other.why, **self.why},
        )


def renders(dialect, action):
    def register(fn):
        _RENDERERS[(dialect, action)] = fn
        return fn

    return register


def secret_renderer(dialect, cls):
    """The secret renderer of `dialect` for a credential class, or None."""
    return next((_RENDERERS[(dialect, c)] for c in cls.__mro__ if (dialect, c) in _RENDERERS), None)


def rendered_classes(dialect) -> list:
    return [c.__name__ for d, c in _RENDERERS if d == dialect and isinstance(c, type)]


def render(dialect, action, *args) -> Preamble:
    if action == "secret":
        storages, credential = args
        renderer = secret_renderer(dialect, type(credential.definition))
        if renderer is None:
            raise UsageError(
                f"the {dialect} dialect cannot render {credential.definition.label} "
                f"({dialect} renders: {', '.join(rendered_classes(dialect)) or 'none'})"
            )
        return renderer(storages, credential)
    try:
        renderer = _RENDERERS[(dialect, action)]
    except KeyError:
        raise UsageError(f"the {dialect} dialect cannot deliver {action}") from None
    return renderer(*args)


def preamble(dialect, definition, inputs) -> Preamble:
    """`definition`'s requires, settings, a secret for each credential input matched to the
    storages among `inputs` it is scoped to, then its init statements. A shell loads what a secret
    needs by itself, as it does for a DuckDB user: a build that has an extension in its repository,
    not linked in, loads it no other way."""
    out = Preamble()
    if definition.requires:
        out += render(dialect, "requires", list(definition.requires))
    if definition.settings:
        out += render(dialect, "settings", dict(definition.settings))
    for credential, storages in pairs(inputs):
        out += render(dialect, "secret", storages, credential)
    return out + Preamble(sql=list(definition.init_sql))


def pairs(inputs):
    """(credential, [storage, ...]) for each credential among `inputs` matched to the storages among
    `inputs` it is scoped to, in the credential's declared scope order. A credential scoped to
    nothing among `inputs` is left out."""
    inputs = list(inputs)
    storages = {r.name: r for r in inputs if r.definition.kind == "storage"}
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


def secret_name(credential) -> str:
    return "ducktest_" + re.sub(r"\W", "_", credential.name)


def _facts(storage):
    return storage.attribute_values()


def _scheme_checked(storages, credential):
    """`storages`, each of a scheme the credential's class is for: a scheme a template gives is
    known only now."""
    cls = type(credential.definition)
    for storage in storages:
        scheme = _facts(storage)["scheme"]
        if scheme not in cls.schemes:
            raise UsageError(
                f"{credential.definition.label} is scoped to storage {storage.name!r} ({storage.root}): an "
                f"{cls.__name__} is for {', '.join(cls.schemes)} storages only"
            )
    return storages


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


def _s3_options(storages, credential, by_name):
    """TYPE, PROVIDER, the fields of the credential by their SDK env names (`by_name`), the connection
    options and SCOPE. With credential_chain the AWS SDK reads the keys from the env itself, so only
    the region is named: an explicit REGION wins over the one the chain finds. SCOPE is the plain
    root for one storage, a list of roots for several: both are valid DuckDB syntax."""
    connections = []
    for storage in _scheme_checked(storages, credential):
        endpoint = _facts(storage)["endpoint"]
        connection = {}
        if endpoint:
            connection["ENDPOINT"] = endpoint.split("://", 1)[-1]
            connection["URL_STYLE"] = "path"
            connection["USE_SSL"] = endpoint.startswith("https://")
        connections.append(connection)
    if any(c != connections[0] for c in connections):
        raise UsageError(
            f"credential {credential.name!r} is scoped to storages {[s.name for s in storages]} on different "
            "endpoints, so one secret cannot cover them"
        )
    roots = [_facts(s)["root"] for s in storages]
    d = credential.definition
    options = {"TYPE": RAW("s3"), "PROVIDER": RAW(d.provider)}
    named = ("region",)
    if d.provider == "credential_chain":
        options["CHAIN"] = "env"
    else:
        named = tuple(type(d).env)
    options.update({name.upper(): by_name(type(d).env[name][0]) for name in named if name in credential.fields})
    return {**options, **connections[0], "SCOPE": roots[0] if len(roots) == 1 else roots}


class RAW(str):
    """An option value written as it is, not quoted: `TYPE s3`."""


def _render_value(value) -> str:
    if isinstance(value, RAW):
        return str(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return "[" + ", ".join(quote(v) for v in value) + "]"
    return quote(value)


def _create_secret(credential, options):
    body = ", ".join(f"{k} {_render_value(v)}" for k, v in options.items())
    return f"CREATE SECRET {secret_name(credential)} ({body});"


def _secret_of(options):
    """The CREATE SECRET statement of `options(storages, credential, by_name)`."""
    return lambda storages, credential, by_name: _create_secret(credential, options(storages, credential, by_name))


def duckdb_secret(cls, statement, extensions):
    """Registers the secret of a credential class for both DuckDB dialects. `statement(storages,
    credential, by_name)` gives its SQL, a field's value as `by_name(its SDK env name)`: the text
    names the variable and the engine reads its value from its own env. The shell calls `getenv()`;
    the binary fills a `{NAME}` from the variables passed through to it. `extensions(credential)`
    gives {extension: why the credential needs it}: the binary loads no extension by itself, so the
    init file requires them first; a shell autoloads them for the secret."""

    def needed(credential):
        why = extensions(credential)
        return {"requires": list(why), "why": why}

    def shell(storages, credential):
        by_name = lambda name: RAW(f"getenv({quote(name)})")  # noqa: E731
        return Preamble(sql=[statement(storages, credential, by_name)], **needed(credential))

    def init_file(storages, credential):
        by_name = lambda name: f"{{{name}}}"  # noqa: E731
        return Preamble(sql=[statement(storages, credential, by_name)], **needed(credential))

    renders("duckdb", cls)(shell)
    renders("unittest", cls)(init_file)


def _s3_extensions(credential):
    label = credential.definition.label
    out = {"httpfs": f"{label} needs the httpfs extension"}
    if credential.definition.provider == "credential_chain":
        out["aws"] = (
            f"{label} needs the aws extension: its secret's PROVIDER credential_chain is the aws extension's; "
            "a build without aws takes provider='config'"
        )
    return out


duckdb_secret(S3CredentialDef, _secret_of(_s3_options), _s3_extensions)

_AZURE_RENDERED = {  # provider -> the fields its secret holds; credential_chain's go to the SDK as env only
    "config": ("connection_string",),
    "service_principal": ("tenant_id", "client_id", "client_secret", "client_certificate_path"),
    "credential_chain": (),
    # The SDK has no env path for a raw bearer token, so the secret must hold it: by name, as the rest.
    "access_token": ("access_token",),
}


def _azure_options(storages, credential, by_name):
    """TYPE, PROVIDER, the provider's fields by their SDK env names, CHAIN, ACCOUNT_NAME and SCOPE. The
    account is the storages' own (an abfss root names it), else the credential's."""
    d = credential.definition
    accounts = {_facts(s)["account"] for s in _scheme_checked(storages, credential)}
    if len(accounts) > 1:
        raise UsageError(
            f"credential {credential.name!r} is scoped to storages {[s.name for s in storages]} of different "
            "accounts, so one secret cannot cover them"
        )
    env = type(d).env
    options = {"TYPE": RAW("azure"), "PROVIDER": RAW(d.provider)}
    options.update(
        {name.upper(): by_name(env[name][0]) for name in _AZURE_RENDERED[d.provider] if name in credential.fields}
    )
    if d.chain is not None:
        options["CHAIN"] = d.chain
    (account,) = accounts
    if account:
        options["ACCOUNT_NAME"] = account
    elif "account_name" in credential.fields:
        options["ACCOUNT_NAME"] = by_name(env["account_name"][0])
    roots = [_facts(s)["root"] for s in storages]
    options["SCOPE"] = roots[0] if len(roots) == 1 else roots
    return options


duckdb_secret(
    AzureCredentialDef, _secret_of(_azure_options), lambda c: {"azure": f"{c.definition.label} needs the azure extension"}
)


# -----------------------------------------------------------------------------
# unittest
#


@renders("unittest", "requires")
def _unittest_requires(names):
    return Preamble(sql=[f"require {name}" for name in names])


renders("unittest", "settings")(_duckdb_settings)


# -----------------------------------------------------------------------------
# spark
#


@renders("spark", "requires")
def _spark_requires(names):
    raise UsageError(f"a Spark session cannot load {names} at run time; its packages are given when it boots")


@renders("spark", "settings")
def _spark_settings(settings):
    return Preamble(conf={name: str(value) for name, value in settings.items()})


@renders("spark", S3CredentialDef)
def _spark_s3(storages, credential):
    """Settings of each scoped bucket, so buckets of several stores coexist. The keys come from the
    JVM's env, given at boot, through the env provider. They are set on a running session, where the
    Hadoop configuration takes them without the `spark.hadoop.` prefix that configures one at boot."""
    from ..engines.spark import env_provider

    region = credential.fields.get("region")
    conf = {}
    for storage in _scheme_checked(storages, credential):
        facts = _facts(storage)
        bucket = facts["root"].split("://", 1)[1].split("/", 1)[0]
        per_bucket = {"aws.credentials.provider": env_provider()}
        if region is not None:
            per_bucket["endpoint.region"] = region.get()
        if facts["endpoint"]:
            per_bucket["endpoint"] = facts["endpoint"]
            per_bucket["path.style.access"] = "true"
            per_bucket["connection.ssl.enabled"] = str(facts["endpoint"].startswith("https://")).lower()
        conf.update({f"fs.s3a.bucket.{bucket}.{k}": v for k, v in per_bucket.items()})
    conf["fs.s3.impl"] = (
        "org.apache.hadoop.fs.s3a.S3AFileSystem"  # so a body gives Spark the `s3://` URI it gives DuckDB
    )
    return Preamble(conf=conf)
