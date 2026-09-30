"""The one shape every resource has: a `FooDef` declared once, a `Foo` the run made from it."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Mapping

from ..binary import UsageError

EXCLUSIVE, SHARED_RW, SHARED_RO = "exclusive", "shared_rw", "shared_ro"
RW, RO = "rw", "ro"
NEVER, ON_FAILURE, ALWAYS = "never", "on_failure", "always"


@dataclass(frozen=True)
class Attribute:
    """One named attribute a resource made, and what gave it. `value` is a value, a getter called at delivery, or None:
    the name is unset."""

    value: Any
    source: str

    def get(self):
        return self.value() if callable(self.value) else self.value

    @property
    def unset(self) -> bool:
        return self.value is None


def canonical(value):
    """`value` as plain JSON data that is the same in every process: a callable is its qualified name."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if hasattr(value, "content"):
        return canonical(value.content())
    if callable(value):
        return f"{value.__module__}.{value.__qualname__}"
    raise TypeError(f"{type(value).__name__} has no stable content; give it a content() method")


def digest(doc) -> str:
    return hashlib.sha256(json.dumps(canonical(doc), sort_keys=True).encode()).hexdigest()


class ResourceDef:
    """A declaration of a resource, interpreted by its kind. A kind is a dataclass with `id` first,
    then its own fields, then `inputs` and `retain`; `make()` turns it into an instance.

    `inputs` holds ids or Def objects; a bare entry is a read-write edge, `{"x": "ro"}` a read-only
    one. `retain` says when the run leaves what it made in place: never, on_failure, always."""

    kind: ClassVar[str] = "resource"
    sharing: ClassVar[str] = SHARED_RW  # the kind declares its sharing; the edge declares its mode
    mints_children: ClassVar[bool] = False  # an exclusive acquirer gets a child instead of the parent lock
    instance: ClassVar[Callable[..., Resource]]
    not_content: ClassVar[tuple] = ("id", "inputs", "retain")  # the name is a label, not what it is

    id: str
    inputs: list
    retain: str

    def edges(self) -> list[tuple[str, str, ResourceDef | None]]:
        """(id, mode, the Def when the entry was one) for each input."""
        out = []
        for entry in self.inputs:
            for ref, mode in entry.items() if isinstance(entry, Mapping) else [(entry, RW)]:
                if mode not in (RW, RO):
                    raise UsageError(f"{self.id}: input {ref!r} has mode {mode!r}; it is {RW!r} or {RO!r}")
                d = ref if isinstance(ref, ResourceDef) else None
                out.append((d.id if d else ref, mode, d))
        return out

    def content(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name not in self.not_content}

    def identity(self, inputs: Mapping[str, str] | None = None, mode: str = RW) -> str:
        """sha(kind, content, identities of the fulfilled inputs, mode). `inputs` maps each input id
        to the identity of the instance that fulfilled it. A minted child's identity is not this: it
        is sha(parent identity, key), given to the child where it is minted (see `mint_child`)."""
        inputs = dict(inputs or {})
        missing = [ref for ref, _, _ in self.edges() if ref not in inputs]
        if missing:
            raise ValueError(f"{self.id}: inputs {missing} are not fulfilled")
        edges = sorted([inputs[ref], m] for ref, m, _ in self.edges())
        return digest({"kind": self.kind, "content": self.content(), "inputs": edges, "mode": mode})

    def make(
        self,
        inputs: Mapping[str, Resource] | None = None,
        *,
        run_token: str | None = None,
        mode: str = RW,
    ) -> Resource:
        """The instance for these fulfilled inputs. Nothing is allocated yet: that is `create()`."""
        inputs = dict(inputs or {})
        identity = self.identity({k: r.identity for k, r in inputs.items()}, mode)
        return self.instance(self, identity, run_token, inputs)


class Resource:
    """What the run made from a `ResourceDef`: lifecycle hooks, ownership, and its attributes: what
    it made, known once it exists and never part of its identity.

    Two maps, `attributes` and `secrets`, name -> `Attribute`, named as the kind names them;
    `delivery()` gives them the names a body reads. A secret is never written to a file."""

    owned: ClassVar[bool] = False  # the run makes and removes it: it carries `run_token` and `list(run_token)` finds it

    def __init__(self, definition, identity: str, run_token: str | None, inputs: Mapping[str, Resource] | None = None):
        self.definition = definition
        self.identity = identity
        self.run_token = (
            run_token  # one value per ducktest invocation, on everything the run makes, so a stopped run can be swept
        )
        self.inputs = dict(inputs or {})
        self.attributes: dict[str, Attribute] = {}
        self.secrets: dict[str, Attribute] = {}
        self.children: dict[str, Resource] = {}  # key -> the child minted for it
        self._lock = threading.Condition()
        self._readers, self._writer = 0, False

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def handle(self) -> str:
        """What `list(run_token)` returns for this instance."""
        return self.identity

    # -- lifecycle hooks: a kind overrides what it has --------------------------
    def alloc(self): ...
    def dealloc(self): ...
    def start(self): ...
    def stop(self): ...

    def ready(self) -> bool:
        return True

    def expires_at(self) -> float | None:
        """When the attributes stop being valid, as a time.time(); None for never."""
        return None

    def renew(self): ...

    def mint_child(self, key: str) -> Resource | None:
        """What an exclusive acquirer gets instead of a lock, or None: the kind locks. Give the
        child identity `digest([self.identity, key])`: `key` is not part of a Def's own identity."""
        return None

    # -- the handler contract -------------------------------------------------
    def create(self):
        """Safe to call again after a failure, or when it exists."""
        self.alloc()
        self.start()

    def delete(self):
        """Removing what is not there is not an error."""
        self.stop()
        self.dealloc()

    def read(self) -> dict | None:
        """A description of the instance as it exists, or None when it does not."""
        return {"id": self.id, "identity": self.identity}

    def list(self, run_token: str) -> list[str]:
        """The handles of what carries `run_token`, made from this instance."""
        return []

    def retained(self, failed: bool) -> bool:
        policy = self.definition.retain
        return policy == ALWAYS or (policy == ON_FAILURE and failed)

    # -- ownership ------------------------------------------------------------
    def _exclusive(self, mode):
        return mode == RW and (self.definition.sharing == EXCLUSIVE or self.definition.mints_children)

    def acquire(self, mode: str = RW, key: str | None = None) -> Resource:
        """The instance to use: this one, shared or locked, or a child minted for `key`. A
        shared_ro kind serves a rw edge as a read: there is nothing in it to write."""
        expires_at = self.expires_at()
        if expires_at is not None and expires_at <= time.time():
            self.renew()
        if self._exclusive(mode) and key is not None:
            with self._lock:
                child = self.children.get(key) or self.mint_child(key)
                if child is not None:
                    self.children[key] = child
                    return child
        with self._lock:
            if self._exclusive(mode):
                self._lock.wait_for(lambda: not self._writer and not self._readers)
                self._writer = True
            else:
                self._lock.wait_for(lambda: not self._writer)
                self._readers += 1
        return self

    def release(self, held: Resource, mode: str = RW):
        if held is not self:
            return  # a child stays until the run deletes it
        with self._lock:
            if self._exclusive(mode):
                self._writer = False
            else:
                self._readers -= 1
            self._lock.notify_all()

    # -- attributes -----------------------------------------------------------
    def attribute_values(self) -> dict:
        """Its attributes, by name, as values; never its secrets."""
        return {name: a.get() for name, a in self.attributes.items()}

    def by_ref(self) -> dict[str, Attribute]:
        """Every attribute, secrets too, under this instance's own prefix: `<id>.<name>`."""
        return {f"{self.id}.{name}": a for name, a in {**self.attributes, **self.secrets}.items()}

    def delivery(self) -> tuple[dict[str, Attribute], dict[str, Attribute]]:
        """(values, secrets) under the names a body reads."""
        return dict(self.attributes), dict(self.secrets)


ResourceDef.instance = Resource
