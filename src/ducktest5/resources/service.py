"""Services: something that runs and that a test reaches at an address -- a file server, an object
store, a catalog. The run starts one instance of each declaration and ends it with the run, or finds
one already running: `.as_existing(address=...)`. A test reaches one through its inputs: a storage
on it, a credential it produces, or the service itself."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import ClassVar, Mapping

from ..host import wait_until
from .base import SHARED_RW, Output, Resource, ResourceDef
from .credential import CredentialDef


class ServiceNotUp(RuntimeError):
    """A service did not come up, or an existing one does not answer: the Tests that need it are BLOCKED."""


class ServiceDef(ResourceDef):
    """A service. A kind is a dataclass with `id`, its own fields, then `address: str | None = None`,
    `inputs` and `retain`, with an instance class that is a `Service`. `retain` is `on_failure` by
    convention: a failed run leaves what the service holds in place, stopped.

    `attributes` names what `read()` of a running instance gives its consumers, as `<id>.<name>`
    (`http.url`). `secret_attributes` are read the same way but reach a consumer only through a
    credential the service produces (`credential()`), under the names `delivers` gives them.
    `detached` says an instance outlives the ducktest process that started it (a container), so
    `ducktest5 provision` may make it apart from the exec that uses it; one that lives in this
    process (a thread) ends with it."""

    kind = "service"
    sharing = SHARED_RW
    detached: ClassVar[bool] = False
    attributes: ClassVar[tuple] = ()
    secret_attributes: ClassVar[tuple] = ()
    delivers: ClassVar[Mapping[str, str]] = {}  # delivered name -> attribute or secret attribute
    ready_timeout_s: ClassVar[float] = 60

    address: str | None

    def as_existing(self, address: str) -> ServiceDef:
        """The same service found running at `address`: the run never starts it, and never removes it."""
        return dataclasses.replace(self, address=address)

    def credential(self, id: str, scopes: list, **fields) -> CredentialDef:
        """A credential made of this service's secrets, scoped to `scopes`: the storages it serves."""
        source = FromService(self.id, tuple(sorted(self.delivers.items())))
        return CredentialDef(id, sources=[source], scopes=scopes, inputs=[self], **fields)


@dataclass(frozen=True)
class FromService:
    """A credential source that reads a service's attributes when the credential is acquired."""

    service: str
    names: tuple  # ((delivered name, attribute), ...)

    def content(self):
        return {"service": self.service, "names": dict(self.names)}

    def __str__(self):
        return f"service {self.service}"

    def __call__(self, inputs):
        found = {**inputs[self.service].env, **inputs[self.service].secrets}
        return {name: found[attribute].get() for name, attribute in self.names}


class Service(Resource):
    """A service instance. A kind overrides `alloc`/`dealloc` (what it keeps: a directory, a
    volume), `start`/`stop` (what runs), `read` (its attributes while it exists, else None) and
    `ready`. An existing one is only read: create adopts it when it answers, delete leaves it."""

    @property
    def existing(self) -> bool:
        return self.definition.address is not None

    @property
    def owned(self):
        return not self.existing

    @property
    def address(self) -> str | None:
        """Where it answers: what `as_existing` takes to find this instance again."""
        return self.definition.address

    @property
    def attributes(self) -> dict:
        """Its attributes, by name, while it is up; never its secrets."""
        return {name: out.get() for name, out in self.env.items()}

    def _up(self) -> bool:
        return self.read() is not None and self.ready()

    def create(self):
        if not self._up():
            if self.existing:
                raise ServiceNotUp(f"existing service {self.id!r} does not answer at {self.definition.address}")
            self.alloc()
            try:
                self.start()
                wait_until(self._up, self.definition.ready_timeout_s, lambda: f"service {self.id!r} is not ready")
            except Exception as exc:
                self.delete()
                raise ServiceNotUp(f"{type(exc).__name__}: {exc}") from exc
        found, source = self.read(), f"service {self.id}"
        self.env = {name: Output(found.get(name), source) for name in self.definition.attributes}
        self.secrets = {name: Output(found.get(name), source) for name in self.definition.secret_attributes}

    def delete(self):
        if not self.existing:
            super().delete()

    def delivery(self):
        """Its attributes as `<ID>_<NAME>` (`HTTP_URL`); its secrets only through a credential it produces."""
        return {f"{self.id}_{name}".upper(): out for name, out in self.env.items()}, {}

    def end(self, failed: bool):
        """The run is over: stop it, and remove what it holds unless `retain` keeps it."""
        if self.existing:
            return
        self.stop()
        if not self.retained(failed):
            self.dealloc()


ServiceDef.instance = Service
