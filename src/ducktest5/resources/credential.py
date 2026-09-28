"""Credentials: a secret found in a source, or made by a generator, scoped to a storage."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from ..binary import UsageError
from .base import RO, RW, SHARED_RO, Output, Resource, ResourceDef


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


def env(names: Mapping[str, str] | Iterable[str]) -> EnvSource:
    """A source from the process environment: `{"AWS_ACCESS_KEY_ID": "MY_KEY_VAR"}`, or a list of
    names that are delivered as they are called here."""
    pairs = names.items() if isinstance(names, Mapping) else ((n, n) for n in names)
    return EnvSource(tuple(sorted(pairs)))


@dataclass(frozen=True)
class OnePasswordSource:
    item: str

    def content(self):
        return {"1password": self.item}

    def resolve(self, credential_id: str):
        raise UsageError(
            f"credential {credential_id!r}: source 1password:{self.item} is not available here; "
            "put an env(...) source before it"
        )


def _source(entry):
    if isinstance(entry, str) and entry.startswith("1password:"):
        return OnePasswordSource(entry.split(":", 1)[1])
    if isinstance(entry, (EnvSource, OnePasswordSource)):
        return entry
    raise UsageError(f"{entry!r} is not a credential source; use env(...) or '1password:NAME'")


@dataclass(eq=False)
class CredentialDef(ResourceDef):
    """A secret a test needs: the first of `sources` that resolves, or what `generator` makes from
    the credential's inputs when it is acquired. It outputs `secrets` only, never `env`, under the
    names its source gives (`AWS_ACCESS_KEY_ID`). `scope` is the storage it is for, and that
    storage's credential when a test names none; with no scope it binds only by name."""

    kind = "credential"
    sharing = SHARED_RO

    id: str
    sources: list = field(default_factory=list)  # env({...}) or "1password:NAME", tried in order
    scope: object = None  # a storage id, or (storage id, "ro" | "rw"); None: bound only by name
    generator: Callable | None = None  # generator(inputs: {id: Resource}) -> {name: value}, at acquire
    default: bool = False  # the one to pick when several share a scope
    inputs: list = field(default_factory=list)
    retain: str = "on_failure"

    not_content = ResourceDef.not_content + ("default",)

    def __post_init__(self):
        self.sources = [_source(s) for s in self.sources]
        if self.sources and self.generator:
            raise UsageError(f"credential {self.id!r}: sources or a generator, not both")
        if self.scope is not None and self.scope_mode not in (RO, RW):
            raise UsageError(f"credential {self.id!r}: scope mode {self.scope_mode!r}; it is {RO!r} or {RW!r}")

    @property
    def names(self) -> tuple:
        """The names its sources deliver, as far as they are known before it is acquired."""
        return tuple(sorted({name for s in self.sources if isinstance(s, EnvSource) for name, _ in s.names}))

    @property
    def scope_storage(self) -> str | None:
        return self.scope[0] if isinstance(self.scope, (tuple, list)) else self.scope

    @property
    def scope_mode(self) -> str:
        return self.scope[1] if isinstance(self.scope, (tuple, list)) else RW


class Credential(Resource):
    """A resolved credential. Its secrets are getters, read when delivered; a generated one is
    made again at every acquire."""

    def start(self):
        for source in self.definition.sources:
            found = source.resolve(self.id)
            if found is not None:
                self.secrets = found
                return
        if self.definition.sources:
            shown = ", ".join(str(s.content()) for s in self.definition.sources)
            raise UsageError(f"credential {self.id!r}: none of its sources resolved: {shown}")

    def acquire(self, mode=RO, key=None):
        held = super().acquire(mode, key)
        generator = self.definition.generator
        if generator is not None:
            source = f"generator {generator.__qualname__}"
            self.secrets = {name: Output(v, source) for name, v in generator(self.inputs).items()}
        return held

    def read(self):
        return {"id": self.id, "names": sorted(self.secrets)}


CredentialDef.instance = Credential


def pick_credentials(
    storages: Mapping[str, str],
    declared: Iterable[CredentialDef],
    named: Iterable[CredentialDef] = (),
) -> list[CredentialDef]:
    """The credentials of one test. `storages` maps each storage id it uses to its edge mode.
    A named credential wins over scope; a storage with no named one gets the credential scoped to
    it, or the one flagged `default` when several are; with none flagged, which is a usage error."""
    chosen = list(named)
    covered = {c.scope_storage for c in chosen}
    for storage, mode in storages.items():
        if storage in covered:
            continue
        matches = [c for c in declared if c.scope_storage == storage and (c.scope_mode == RW or mode == RO)]
        if len(matches) > 1:
            defaults = [c for c in matches if c.default]
            if len(defaults) != 1:
                ids = sorted(c.id for c in matches)
                raise UsageError(f"storage {storage!r} has credentials {ids} and none is named or flagged default=True")
            matches = defaults
        chosen += matches
    return chosen
