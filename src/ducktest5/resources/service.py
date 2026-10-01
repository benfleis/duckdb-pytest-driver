"""Services: something that runs and that a test reaches at an address -- a file server, an object
store, a catalog. The run starts one instance of each declaration and ends it with the run, or finds
one already running: `.as_existing(address=...)`, or one that answers where it would start it. A
test reaches one through its inputs: a storage on it, a credential it produces, or the service itself."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import ClassVar, Mapping

from ..host import wait_until
from ..binary import UsageError
from .base import SHARED_RW, Attribute, Resource, ResourceDef
from .credential import CredentialDef

_log = logging.getLogger(__name__)


class ServiceNotUp(RuntimeError):
    """A service did not come up, or an existing one does not answer: the Tests that need it are BLOCKED."""


class ServiceDef(ResourceDef):
    """A service. A kind is a dataclass with `id`, its own fields, then `address: str | None = None`,
    `inputs` and `retain`, with an instance class that is a `Service`. `retain` is `on_failure` by
    convention: a failed run leaves what the service holds in place, stopped.

    `attributes` names what `read()` of a running instance gives its consumers, as `<id>.<name>`
    (`http.url`). `secret_attributes` are read the same way but reach a consumer only through a
    credential the service produces (`credential()`): one of `credential_type`, whose fields `delivers`
    maps to attributes.
    `detached` says an instance outlives the ducktest process that started it (a container), so
    `ducktest5 provision` may make it apart from the exec that uses it; one that lives in this
    process (a thread) ends with it."""

    kind = "service"
    sharing = SHARED_RW
    detached: ClassVar[bool] = False
    attributes: ClassVar[tuple] = ()
    secret_attributes: ClassVar[tuple] = ()
    credential_type: ClassVar[type | None] = None  # the CredentialDef class of `credential()`
    delivers: ClassVar[Mapping[str, str]] = {}  # field of credential_type -> attribute or secret attribute
    ready_timeout_s: ClassVar[float] = 60
    provisioned: ClassVar[bool] = (
        True  # False: not made by the run's provisioning (an engine, made where a test leases it)
    )

    address: str | None

    def as_existing(self, address: str) -> ServiceDef:
        """The same service found running at `address`: the run never starts it, and never removes it."""
        return dataclasses.replace(self, address=address)

    def reached_at(self, address: str) -> ServiceDef:
        """The same service found running at `address`, made only to reach what is on it (cleanup of a
        storage after the run): a kind drops there what it would make of its own."""
        return self.as_existing(address)

    def parts_needed(self, inputs) -> list:
        """The parts of this service (an S3 server's buckets) a test with `inputs` needs. A part its
        instance refused (`Service.refused`) blocks only the tests that need it."""
        return []

    def credential(self, id: str, scopes: list, **fields) -> CredentialDef:
        """A credential made of this service's secrets, scoped to `scopes`: the storages it serves."""
        cls = self.credential_type
        if cls is None:
            raise UsageError(f"service {self.id!r} ({type(self).__name__}) produces no credential")
        if unknown := sorted(set(self.delivers) - set(cls.fields)):
            raise UsageError(f"service {self.id!r}: delivers {unknown}, not fields of {cls.__name__}")
        source = FromService(self.id, tuple(sorted(self.delivers.items())))
        return cls(id, sources=[source], scopes=scopes, inputs=[self], **fields)


@dataclass(frozen=True)
class FromService:
    """A credential source that reads a service's attributes when the credential is acquired."""

    service: str
    names: tuple  # ((field, attribute), ...)

    def content(self):
        return {"service": self.service, "names": dict(self.names)}

    def __str__(self):
        return f"service {self.service}"

    def __call__(self, inputs):
        found = {**inputs[self.service].attributes, **inputs[self.service].secrets}
        return {name: found[attribute].get() for name, attribute in self.names}


class Service(Resource):
    """A service instance. A kind overrides `alloc`/`dealloc` (what it keeps: a directory, a
    volume), `start`/`stop` (what runs), `read` (its attributes while it exists, else None) and
    `ready`. An existing one is only read: create adopts it when it answers, delete leaves it.

    A kind whose address is known before it starts may also override `answers`: create then adopts
    one that already answers there, found running, as it would an existing one."""

    found = False  # adopted by create: it answered before the run started anything
    refused: Mapping[str, str] = {}  # part -> why the run does not use it; see ServiceDef.parts_needed
    _started = False

    @property
    def existing(self) -> bool:
        return self.definition.address is not None or self.found

    @property
    def owned(self):
        return not self.existing

    @property
    def address(self) -> str | None:
        """Where it answers: what `as_existing` takes to find this instance again."""
        return self.definition.address

    def answers(self) -> bool:
        """Whether something already answers where this instance is reached, before the run starts it."""
        return False

    def _up(self) -> bool:
        return self.read() is not None and self.ready()

    def create(self):
        if not self.existing and not self._started and self.answers():
            self.found = True
            _log.info("service %s: found running at %s, used as it is", self.id, self.address)
        if not self._up():
            if self.existing:
                raise ServiceNotUp(f"existing service {self.id!r} does not answer at {self.address}")
            self.alloc()
            self._started = True
            try:
                self.start()
                wait_until(self._up, self.definition.ready_timeout_s, lambda: f"service {self.id!r} is not ready")
            except Exception as exc:
                self.delete()
                if isinstance(exc, ServiceNotUp):
                    raise
                raise ServiceNotUp(f"{type(exc).__name__}: {exc}") from exc
        found, source = self.read(), f"service {self.id}"
        self.attributes = {name: Attribute(found.get(name), source) for name in self.definition.attributes}
        self.secrets = {name: Attribute(found.get(name), source) for name in self.definition.secret_attributes}

    def delete(self):
        if not self.existing:
            super().delete()

    def delivery(self):
        """Its attributes as `<ID>_<NAME>` (`HTTP_URL`); its secrets only through a credential it produces."""
        return {f"{self.id}_{name}".upper(): a for name, a in self.attributes.items()}, {}

    def end(self, failed: bool):
        """The run is over: stop it, and remove what it holds unless `retain` keeps it."""
        if self.existing:
            return
        self.stop()
        if not self.retained(failed):
            self.dealloc()


ServiceDef.instance = Service
