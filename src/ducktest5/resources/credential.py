"""Credentials: a secret from the first of an ordered list of sources, scoped to one or more storages."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ..binary import UsageError
from .base import RO, RW, SHARED_RO, Output, Resource, ResourceDef, canonical


@dataclass(frozen=True)
class EnvSource:
    """Secrets read from this process's environment: delivered name -> variable name."""

    names: tuple  # ((delivered name, variable), ...)

    def content(self):
        return {"env": dict(self.names)}

    def resolve(self, credential_id: str) -> dict | None:
        """Getters, not values: the value is read at delivery. None when a variable is not set."""
        if not all(var in os.environ for _, var in self.names):
            return None
        return {name: Output(lambda var=var: os.environ.get(var), f"env {var}") for name, var in self.names}


def from_env(names: Mapping[str, str] | Iterable[str]) -> EnvSource:
    """A source from the process environment. A mapping reads each key and delivers it under its
    value, `{"MY_KEY_VAR": "AWS_ACCESS_KEY_ID"}`; a list delivers each name as it is."""
    pairs = ((out, var) for var, out in names.items()) if isinstance(names, Mapping) else ((n, n) for n in names)
    return EnvSource(tuple(sorted(pairs)))


@dataclass(frozen=True)
class OnePasswordSource:
    item: str

    def content(self):
        return {"1password": self.item}

    def resolve(self, credential_id: str):
        raise UsageError(
            f"credential {credential_id!r}: source 1password:{self.item} is not available here; "
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


def _scope(credential_id, entry):
    """One `scopes` entry, normalized to (storage id, mode)."""
    if isinstance(entry, (tuple, list)):
        if len(entry) != 2:
            raise UsageError(
                f"credential {credential_id!r}: scope {entry!r} is a storage id, or (storage id, {RO!r}|{RW!r})"
            )
        storage, mode = entry
    else:
        storage, mode = entry, RW
    if mode not in (RO, RW):
        raise UsageError(f"credential {credential_id!r}: scope mode {mode!r}; it is {RO!r} or {RW!r}")
    return storage, mode


@dataclass(eq=False)
class CredentialDef(ResourceDef):
    """A secret a test needs. `sources` is one ordered list, tried at acquire: a `from_env(...)` source,
    a string reference with a known scheme (`"1password:NAME"`; `"env:NAME"` is shorthand for
    `from_env([NAME])`), or a callable. A callable is called with the credential's resolved inputs (as
    `from_env(...)` sources are) and returns the secret mapping, or None to try the next source; it is
    late-bound, called at acquire and never at plan. It outputs `secrets` only, never `env`, under
    the names its source gives (`AWS_ACCESS_KEY_ID`). `scopes` lists the storages it is for -- each
    a storage id, or `(storage id, "ro" | "rw")` -- and it is that storage's credential when a test
    names none; a credential may be scoped to several storages, and an empty list binds only by
    name. When a source made something (a callable that logs in), `dealloc` is its revoke or
    logout, and `retain` decides whether it runs; a credential is not kept after a run by default."""

    kind = "credential"
    sharing = SHARED_RO

    id: str
    sources: list = field(default_factory=list)  # from_env(...), "1password:NAME"/"env:NAME", or a callable, in order
    scopes: list = field(default_factory=list)  # storage id, or (storage id, "ro" | "rw"), each
    default: bool = False  # the one to pick when several share a scope
    inputs: list = field(default_factory=list)
    retain: str = "never"

    not_content = ResourceDef.not_content + ("default",)

    def __post_init__(self):
        self.sources = [_source(s) for s in self.sources]
        self.scopes = [_scope(self.id, s) for s in self.scopes]

    @property
    def names(self) -> tuple:
        """The names its sources deliver, as far as they are known before it is acquired."""
        return tuple(sorted({name for s in self.sources if isinstance(s, EnvSource) for name, _ in s.names}))

    @property
    def scope_storages(self) -> tuple:
        """The storage ids it is scoped to, in declared order."""
        return tuple(s for s, _ in self.scopes)

    def scope_mode(self, storage: str) -> str:
        return next(m for s, m in self.scopes if s == storage)


class Credential(Resource):
    """A resolved credential. Its secrets are getters, read when delivered; one a callable source
    makes is made again at every acquire."""

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
        for source in self.definition.sources:
            if callable(source):
                found = source(self.inputs)
                if found is None:
                    continue
                self.secrets = {name: Output(v, f"callable {source.__qualname__}") for name, v in found.items()}
                return
            found = source.resolve(self.id)
            if found is not None:
                self.secrets = found
                return
        if self.definition.sources:
            shown = ", ".join(str(canonical(s)) for s in self.definition.sources)
            raise UsageError(f"credential {self.id!r}: none of its sources resolved: {shown}")

    def read(self):
        return {"id": self.id, "names": sorted(self.secrets)}


CredentialDef.instance = Credential


def pick_credentials(
    storages: Mapping[str, str],
    declared: Iterable[CredentialDef],
    named: Iterable[CredentialDef] = (),
) -> list[CredentialDef]:
    """The credentials of one test. `storages` maps each storage id it uses to its edge mode. A
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
                ids = sorted(c.id for c in matches)
                raise UsageError(f"storage {storage!r} has credentials {ids} and none is named or flagged default=True")
            matches = defaults
        for c in matches:
            if c not in chosen:
                chosen.append(c)
    return chosen
