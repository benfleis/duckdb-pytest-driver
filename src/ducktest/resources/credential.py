"""Credentials: typed by their class, made of fields, from the first of an ordered list of sources,
scoped to one or more storages.

A class names its DuckDB secret `type`, the storage `schemes` it can be scoped to, its `fields` (DuckDB's
secret options in lower case), the SDK env names of each field (`env`), and the name prefixes its SDK
reads (`sdk_prefixes`). The run delivers a credential to every engine process of a test as process
env, under the SDK names: an SDK reads them with getenv(), whether or not a file names them.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from typing import ClassVar, Iterable, Mapping

from ..binary import UsageError
from .base import RO, RW, SHARED_RO, Attribute, Resource, ResourceDef, canonical


@dataclass(frozen=True)
class Field:
    """One field of a credential class: `required` by every source, `secret` masked wherever it is shown."""

    required: bool = False
    secret: bool = False


@dataclass(frozen=True)
class EnvSource:
    """Fields read from this process's environment. `mapped`: `names` is ((variable, field), ...);
    else it is (variable, ...), each an SDK env name of the class."""

    names: tuple
    mapped: bool

    def content(self):
        return {"env": dict(self.names) if self.mapped else list(self.names)}

    def __str__(self):
        return "env " + ", ".join(var for var, _ in self.names) if self.mapped else "env " + ", ".join(self.names)

    def shown(self):
        return f"from_env({dict(self.names)!r})" if self.mapped else f"from_env({list(self.names)!r})"

    def variables(self, credential) -> dict:
        """field -> (variable, ...), the first one set giving the field; a usage error for a variable
        `credential`'s class does not know, or a source that misses a required field."""
        cls = type(credential)
        out = {}
        if self.mapped:
            for var, name in self.names:
                if name not in cls.fields:
                    raise UsageError(
                        f"{credential.label}: from_env maps {var} to {name!r}, which is not a field of "
                        f"{cls.__name__} ({', '.join(cls.fields)})"
                    )
                out.setdefault(name, ())
                out[name] += (var,)
        else:
            by_var = {var: name for name, names in cls.env.items() for var in names}
            for var in self.names:
                if var not in by_var:
                    known = ", ".join(v for names in cls.env.values() for v in names)
                    raise UsageError(
                        f"{credential.label}: {self.shown()}: {var} is not an env name of {cls.__name__} ({known}); "
                        f"map it with from_env({{{var!r}: field}})"
                    )
                out.setdefault(by_var[var], ())
                out[by_var[var]] += (var,)
            # The class's own order: AWS_REGION before AWS_DEFAULT_REGION, whatever order the list gives.
            out = {name: tuple(v for v in cls.env[name] if v in out[name]) for name in out}
        if gap := credential.missing({*out, *credential.literals()}):
            raise UsageError(
                f"{credential.label}: source {self.shown()} gives no {gap}; a source must give every "
                f"required field ({credential.needs_shown()})"
            )
        return out

    def resolve(self, credential) -> dict | None:
        """Getters, not values: a value is read at delivery. None when a required field is not set."""
        out = {}
        for name, variables in self.variables(credential).items():
            var = next((v for v in variables if v in os.environ), None)
            if var is not None:
                out[name] = Attribute(lambda var=var: os.environ.get(var), f"env {var}")
        return None if credential.missing({*out, *credential.literals()}) else out


def from_env(names: Mapping[str, str] | Iterable[str]) -> EnvSource:
    """A source from the process environment. A mapping reads each variable into the field it names,
    `{"MY_KEY_ID": "key_id"}`; a list reads SDK env names of the credential's class, `["AWS_ACCESS_KEY_ID", ...]`."""
    if isinstance(names, Mapping):
        return EnvSource(tuple(sorted(names.items())), True)
    return EnvSource(tuple(names), False)


@dataclass(frozen=True)
class OnePasswordSource:
    item: str

    def content(self):
        return {"1password": self.item}

    def resolve(self, credential):
        raise UsageError(
            f"credential {credential.name!r}: source 1password:{self.item} is not available here; "
            "put a from_env(...) source before it"
        )


_SIMPLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _source(entry):
    if callable(entry):
        return entry
    if isinstance(entry, (EnvSource, OnePasswordSource)):
        return entry
    if isinstance(entry, str):
        if entry.startswith("1password:"):
            return OnePasswordSource(entry.split(":", 1)[1])
        if entry.startswith("env:") and _SIMPLE_NAME.fullmatch(entry[len("env:") :]):
            return from_env([entry[len("env:") :]])
        raise UsageError(
            f"{entry!r}: a string source names where a secret lives, e.g. '1password:NAME'; never the secret itself"
        )
    raise UsageError(f"{entry!r} is not a credential source; use from_env(...), a string reference, or a callable")


def _scope(credential_name, entry):
    """One `scopes` entry, normalized to (storage name, mode); the storage is its name or the StorageDef."""
    if isinstance(entry, (tuple, list)):
        if len(entry) != 2:
            raise UsageError(
                f"credential {credential_name!r}: scope {entry!r} is a storage name, or (storage name, {RO!r}|{RW!r})"
            )
        storage, mode = entry
    else:
        storage, mode = entry, RW
    from .. import names

    storage = names.ref(storage, "storage", f"credential {credential_name!r}: scopes")
    if mode not in (RO, RW):
        raise UsageError(f"credential {credential_name!r}: scope mode {mode!r}; it is {RO!r} or {RW!r}")
    return storage, mode


@dataclass(eq=False)
class CredentialDef(ResourceDef):
    """A secret a test needs, of a typed class (`S3CredentialDef`); this base is never declared itself.

    `sources` is one ordered list, tried at acquire, and the first that resolves gives all the fields:
    `from_env(...)`, a string reference with a known scheme (`"1password:NAME"`; `"env:NAME"` is
    shorthand for `from_env([NAME])`), or a callable. A callable is called with the credential's
    resolved inputs and returns `{field: value}`, or None to try the next source; it is late-bound,
    called at acquire and never at plan. A non-secret field of the class may be given as a literal
    (`region="eu-west-1"`): a default that a source overrides.

    `scopes` lists the storages it is for -- each a storage name, or `(storage name, "ro" | "rw")` -- and
    it is that storage's credential when a test names none; an empty list binds only by name.
    `values` are plain `{NAME}` values a test gets when it holds it, such as the signal an existing
    `.test` file checks with `require-env`. When a source made something (a callable that logs in),
    `dealloc` is its revoke or logout, and `retain` decides whether it runs."""

    kind = "credential"
    sharing = SHARED_RO
    type: ClassVar[str] = ""  # the DuckDB secret TYPE
    schemes: ClassVar[tuple] = ()  # the storage schemes it can be scoped to
    fields: ClassVar[dict] = {}  # DuckDB option in lower case -> Field
    env: ClassVar[dict] = {}  # field -> its SDK env names: read, the first that is set; delivered, all
    sdk_prefixes: ClassVar[tuple] = ()  # what engines never take from the shell's env

    name: str
    sources: list = field(default_factory=list)
    scopes: list = field(default_factory=list)  # storage name, or (storage name, "ro" | "rw"), each
    default: bool = False  # the one to pick when several share a scope
    values: dict = field(default_factory=dict)
    inputs: list = field(default_factory=list)
    retain: str = "never"

    not_content = ResourceDef.not_content + ("default",)

    def __post_init__(self):
        if type(self) is CredentialDef:
            raise UsageError(
                f"CredentialDef({self.name!r}): CredentialDef is the base class; declare a typed credential, "
                "such as S3CredentialDef"
            )
        super().__post_init__()
        from .client import check_names

        self.sources = [_source(s) for s in self.sources]
        for source in self.sources:
            if isinstance(source, EnvSource):
                source.variables(self)
        self.scopes = [_scope(self.name, s) for s in self.scopes]
        check_names(self.values, f"{self.label}: values")

    def content(self) -> dict:
        return {"class": type(self).__name__, **super().content()}

    def attribute_names(self) -> tuple:
        return tuple(self.values)

    @property
    def label(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    @classmethod
    def required(cls) -> tuple:
        return tuple(name for name, f in cls.fields.items() if f.required)

    def needs(self) -> tuple:
        """What every source must give: each entry a field, or a tuple of fields one of which will do."""
        return type(self).required()

    def missing(self, given) -> str | None:
        """The first of `needs()` that the field names `given` do not meet, as a message says it."""
        for need in self.needs():
            names = need if isinstance(need, tuple) else (need,)
            if not any(n in given for n in names):
                return " or ".join(repr(n) for n in names)
        return None

    def needs_shown(self) -> str:
        return ", ".join(" or ".join(n) if isinstance(n, tuple) else n for n in self.needs())

    @classmethod
    def secret_env(cls) -> tuple:
        """The SDK env names of its secret fields."""
        return tuple(n for name, f in cls.fields.items() if f.secret for n in cls.env.get(name, ()))

    def literals(self) -> dict:
        """The non-secret fields given as literals on the declaration."""
        return {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name in type(self).fields and getattr(self, f.name) is not None
        }

    @property
    def scope_storages(self) -> tuple:
        """The storage ids it is scoped to, in declared order."""
        return tuple(s for s, _ in self.scopes)

    def scope_mode(self, storage: str) -> str:
        return next(m for s, m in self.scopes if s == storage)


def credential_types() -> list:
    """Every credential class there is, the base left out."""
    out, todo = [], list(CredentialDef.__subclasses__())
    while todo:
        cls = todo.pop(0)
        out.append(cls)
        todo += cls.__subclasses__()
    return out


_inherit: tuple = ()  # the project's inherit_env; the process holds one project, as for its pythonpath


def inherit_env(names, where="inherit_env") -> tuple:
    """Names engines keep from the shell's env although an SDK prefix would remove them. A key is never
    one of them: it comes from a credential."""
    global _inherit
    for name in names:
        owner = next((cls for cls in credential_types() if name in cls.secret_env()), None)
        if owner is not None:
            shell = next((m for m in vars(owner) if m.startswith("from_") and m.endswith("_env")), None)
            how = f"{owner.__name__}.{shell}(...)" if shell else f"an {owner.__name__}"
            raise UsageError(f"{where}: {name} is a credential name; declare {how} instead")
    _inherit = tuple(names)
    return _inherit


def sdk_prefixes() -> tuple:
    return tuple(p for cls in credential_types() for p in cls.sdk_prefixes)


def removed_names(environ=None) -> list:
    """The names of the shell's env that no engine process gets: an SDK reads them, and only a
    credential may give them. `Project(inherit_env=...)` keeps some."""
    environ = os.environ if environ is None else environ
    prefixes = sdk_prefixes()
    return sorted(n for n in environ if n.startswith(prefixes) and n not in _inherit)


def kept_names(environ=None) -> list:
    environ = os.environ if environ is None else environ
    return sorted(n for n in _inherit if n in environ)


def engine_env(environ=None) -> dict:
    """What an engine process starts from: the shell's env without the names an SDK would read."""
    environ = os.environ if environ is None else environ
    gone = set(removed_names(environ))
    return {n: v for n, v in environ.items() if n not in gone}


def _getter(value):
    return lambda: value


class Credential(Resource):
    """A resolved credential: `fields` by name, each a getter read when delivered, and `secrets`, the
    same under the SDK env names of its class. One a callable source makes is made again at every acquire."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        self.fields: dict[str, Attribute] = {}
        self.source = ""  # what gave the fields, for a reader
        source = f"credential {definition.name}"
        self.attributes = {name: Attribute(value, source) for name, value in definition.values.items()}

    def start(self):
        # A callable is late-bound: resolving it (and anything after it, in order) waits for acquire.
        if not any(callable(s) for s in self.definition.sources):
            self._try_sources()

    def acquire(self, mode=RO, key=None):
        held = super().acquire(mode, key)
        if any(callable(s) for s in self.definition.sources):
            self._try_sources()
        return held

    def _try_sources(self):
        """The first of `sources` that resolves, tried in order."""
        d = self.definition
        for source in d.sources:
            if callable(source):
                found = source(self.inputs)
                if found is None:
                    continue
                label = str(source) if hasattr(source, "content") else f"callable {source.__qualname__}"
                self._set(self._checked(found, label), label)
                return
            found = source.resolve(d)
            if found is not None:
                self._set(found, str(source))
                return
        if not d.sources:
            self._set({}, "its declaration")
            return
        shown = ", ".join(str(canonical(s)) for s in d.sources)
        raise UsageError(f"credential {self.name!r}: none of its sources resolved: {shown}")

    def _checked(self, found, label):
        """What a callable gave, as getters: a value never counts toward an identity."""
        cls = type(self.definition)
        if unknown := sorted(set(found) - set(cls.fields)):
            raise UsageError(
                f"{self.definition.label}: {label} gave {unknown}, not fields of {cls.__name__} ({', '.join(cls.fields)})"
            )
        given = {name for name, v in found.items() if v is not None}
        if gap := self.definition.missing({*given, *self.definition.literals()}):
            raise UsageError(f"{self.definition.label}: {label} gave no {gap}, a required field")
        return {name: Attribute(_getter(v), label) for name, v in found.items() if v is not None}

    def _set(self, found, label):
        given = self.definition.literals()
        literal = {n: Attribute(_getter(v), f"credential {self.name}") for n, v in given.items() if n not in found}
        self.fields = {**literal, **found}
        self.source = label
        env = type(self.definition).env
        self.secrets = {name: a for field_name, a in self.fields.items() for name in env.get(field_name, ())}

    def read(self):
        return {"name": self.name, "names": sorted(self.secrets)}


CredentialDef.instance = Credential


@dataclass(eq=False)
class S3CredentialDef(CredentialDef):
    """Keys of an S3 store, or of one that speaks S3. They reach the engine as AWS_* env, and its secret
    reads them there by `provider`:

        credential_chain    the AWS SDK chain reads the env (CHAIN 'env'), as a DuckDB user's secret
                            does; it is a provider of the aws extension, which the test then requires
        config              the secret names each env variable (`getenv()`, a `{NAME}` template): for a
                            build without the aws extension, such as httpfs's own"""

    type = "s3"
    schemes = ("s3",)
    fields = {
        "key_id": Field(required=True, secret=True),
        "secret": Field(required=True, secret=True),
        "session_token": Field(secret=True),
        "region": Field(),
    }
    env = {
        "key_id": ("AWS_ACCESS_KEY_ID",),
        "secret": ("AWS_SECRET_ACCESS_KEY",),
        "session_token": ("AWS_SESSION_TOKEN",),
        "region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
    }
    sdk_prefixes = ("AWS_",)
    providers: ClassVar[tuple] = ("credential_chain", "config")

    provider: str = "credential_chain"
    region: str | None = None

    def __post_init__(self):
        if self.provider not in self.providers:
            raise UsageError(f"{self.label}: provider {self.provider!r}; it is one of {', '.join(self.providers)}")
        super().__post_init__()

    @classmethod
    def from_aws_env(cls, name: str, **fields) -> S3CredentialDef:
        """The keys of the shell's own AWS_* env, said so explicitly."""
        return cls(name, sources=[from_env([n for names in cls.env.values() for n in names])], **fields)


class S3Credential(Credential):
    """A resolved `S3CredentialDef`."""


S3CredentialDef.instance = S3Credential


_SPN = ("tenant_id", "client_id", ("client_secret", "client_certificate_path"))


@dataclass(eq=False)
class AzureCredentialDef(CredentialDef):
    """An Azure storage credential, one of DuckDB's azure secret providers:

        config              a connection string, or only `account_name` (anonymous reads)
        service_principal   tenant_id, client_id and client_secret or client_certificate_path
        credential_chain    the Azure SDK finds them; `chain` ('env', 'cli', 'managed_identity',
                            'cli;env', ...) names where. With chain 'env' a source must give the
                            service principal, which reaches the engine as AZURE_TENANT_ID, ...
        access_token        an OAuth bearer token for storage (`az account get-access-token
                            --resource https://storage.azure.com`)

    The azure extension reads none of these from the env by itself; a `.test` file reads
    `{AZURE_STORAGE_CONNECTION_STRING}` and the SDK's EnvironmentCredential the service principal's
    names. DuckDB gets every provider as a scoped `TYPE azure` secret, a connection string included,
    by reference to the env name. The az cli needs its login: keep `AZURE_CONFIG_DIR` with `Project(inherit_env=...)`."""

    type = "azure"
    schemes = ("az", "abfss")
    fields = {
        "connection_string": Field(secret=True),
        "account_name": Field(),
        "tenant_id": Field(),
        "client_id": Field(),
        "client_secret": Field(secret=True),
        "client_certificate_path": Field(secret=True),
        "access_token": Field(secret=True),
    }
    env = {
        "connection_string": ("AZURE_STORAGE_CONNECTION_STRING",),
        "account_name": ("AZURE_STORAGE_ACCOUNT",),
        "tenant_id": ("AZURE_TENANT_ID",),
        "client_id": ("AZURE_CLIENT_ID",),
        "client_secret": ("AZURE_CLIENT_SECRET",),
        "client_certificate_path": ("AZURE_CLIENT_CERTIFICATE_PATH",),
        "access_token": ("AZURE_ACCESS_TOKEN",),  # ducktest's name, not the SDK's: it reads no token from env
    }
    # The Azure SDK's managed identity reads IDENTITY_*, MSI_* and IMDS_ENDPOINT besides AZURE_*.
    sdk_prefixes = ("AZURE_", "IDENTITY_", "MSI_", "IMDS_")
    providers: ClassVar[dict] = {  # provider -> the fields it takes
        "config": ("connection_string", "account_name"),
        "service_principal": ("tenant_id", "client_id", "client_secret", "client_certificate_path", "account_name"),
        "credential_chain": ("tenant_id", "client_id", "client_secret", "client_certificate_path", "account_name"),
        "access_token": ("access_token", "account_name"),
    }

    provider: str = "config"
    chain: str | None = None
    account_name: str | None = None
    tenant_id: str | None = None
    client_id: str | None = None

    def __post_init__(self):
        if self.provider not in self.providers:
            raise UsageError(f"{self.label}: provider {self.provider!r}; it is one of {', '.join(self.providers)}")
        if self.chain is not None and self.provider != "credential_chain":
            raise UsageError(f"{self.label}: chain is for provider 'credential_chain', not {self.provider!r}")
        super().__post_init__()
        taken = self.providers[self.provider]
        given = {*self.literals()}
        for source in self.sources:
            if isinstance(source, EnvSource):
                given |= set(source.variables(self))
        if other := sorted(given - set(taken)):
            raise UsageError(
                f"{self.label}: provider {self.provider!r} takes {', '.join(taken)}; not {', '.join(other)}"
            )

    def needs(self) -> tuple:
        if self.provider == "config":
            return (("connection_string", "account_name"),)
        if self.provider == "service_principal" or self.chain == "env":
            return _SPN
        if self.provider == "access_token":
            return ("access_token",)
        return ()

    @classmethod
    def from_azure_env(cls, name: str, provider: str = "credential_chain", **fields) -> AzureCredentialDef:
        """The shell's own AZURE_* names that `provider` takes, said so explicitly."""
        names = [n for name in cls.providers[provider] for n in cls.env[name]]
        return cls(name, sources=[from_env(names)], provider=provider, **fields)


class AzureCredential(Credential):
    """A resolved `AzureCredentialDef`."""


AzureCredentialDef.instance = AzureCredential


@dataclass(frozen=True)
class CredentialView:
    """What a body sees of a credential its test holds: never a secret value. `secret_name` is the
    secret the run made for it, None where it made none; `scope` is that secret's scope, as
    `duckdb_secrets()` shows it; `env` the process env names it is delivered under."""

    name: str
    type: str
    secret_name: str | None
    storages: tuple
    scope: list
    env: tuple
    fields: dict
    source: str


def pick_credentials(
    storages: Mapping[str, str],
    declared: Iterable[CredentialDef],
    named: Iterable[CredentialDef] = (),
) -> list[CredentialDef]:
    """The credentials of one test. `storages` maps each storage name it uses to its edge mode. A
    credential matches a storage if any of its `scopes` names it, so one credential may serve
    several storages of one test. A named credential wins over scope; a storage with no named one
    gets the credential scoped to it, or the one flagged `default` when several are; with none
    flagged, which is a usage error (per storage)."""
    chosen = list(named)
    covered = {s for c in chosen for s in c.scope_storages}
    for storage, mode in storages.items():
        if storage in covered:
            continue
        matches = [c for c in declared if storage in c.scope_storages and (c.scope_mode(storage) == RW or mode == RO)]
        if len(matches) > 1:
            defaults = [c for c in matches if c.default]
            if len(defaults) != 1:
                ids = sorted(c.name for c in matches)
                raise UsageError(f"storage {storage!r} has credentials {ids} and none is named or flagged default=True")
            matches = defaults
        for c in matches:
            if c not in chosen:
                chosen.append(c)
    return chosen
