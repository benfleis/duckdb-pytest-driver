"""What one run made: every instance carries the run's `run_token`, and each service is made once
and shared by every consumer, whichever phase made it."""

from __future__ import annotations

import logging
from typing import Iterable

from .base import RW, Resource, ResourceDef
from .service import Service, ServiceDef, ServiceNotUp

_log = logging.getLogger(__name__)

MANAGED, EXISTING, KEPT = "managed", "existing", "kept"


def services_in(definitions: Iterable[ResourceDef]) -> list[ServiceDef]:
    """The services among `definitions` and their inputs, inputs first, each once; an engine is
    not one of them, the services behind its inputs are."""
    found = {}

    def walk(d):
        for _, _, sub in d.edges():
            if sub is not None:
                walk(sub)
        if isinstance(d, ServiceDef) and d.provisioned:
            found.setdefault(d.id, d)

    for d in definitions:
        walk(d)
    return list(found.values())


def planned_identity(definition: ResourceDef, mode: str = RW) -> str:
    """The identity the run's instance of `definition` will have, worked out from the declarations
    alone, as `Made.make` makes it: a service by its own content, anything else by its edge's mode."""
    inputs = {
        ref: planned_identity(sub, RW if isinstance(sub, ServiceDef) else m)
        for ref, m, sub in definition.edges()
        if sub is not None
    }
    return definition.identity(inputs, mode)


class Made:
    """The run's instances. `run_token` is None where nothing is made for a run (scan, plan): a
    service is then never up, and what needs one waits for the run."""

    def __init__(self, run_token: str | None = None):
        self.run_token = run_token
        self.services: dict[str, Service] = {}
        self.modes: dict[str, str] = {}  # service id -> MANAGED, EXISTING or KEPT
        self.errors: dict[str, str] = {}  # service id -> why it is not up

    def make(self, definition: ResourceDef, mode: str = RW) -> Resource:
        """`definition`'s instance for this run, its inputs made first; a service is the run's one."""
        if isinstance(definition, ServiceDef):
            service = self.services.get(definition.id)
            if service is None:
                raise ServiceNotUp(self.errors.get(definition.id) or f"service {definition.id!r} is not up")
            return service
        return definition.make(self.inputs_of(definition), run_token=self.run_token, mode=mode)

    def inputs_of(self, definition: ResourceDef) -> dict:
        return {ref: self.make(sub, mode) for ref, mode, sub in definition.edges() if sub is not None}

    def create(self, definition: ServiceDef, mode: str = MANAGED) -> Service:
        """Start the service, or adopt the existing one; raises ServiceNotUp, and remembers why. A
        KEPT one is left running when the run ends."""
        try:
            service = definition.make(self.inputs_of(definition), run_token=self.run_token)
            service.create()
        except Exception as exc:
            self.errors[definition.id] = str(exc) if isinstance(exc, ServiceNotUp) else f"{type(exc).__name__}: {exc}"
            raise ServiceNotUp(self.errors[definition.id]) from exc
        self.services[definition.id] = service
        self.modes[definition.id] = mode
        return service

    def end(self, failed: bool, on_error=None):
        """Every service ends, the last one made first, except a kept one. One that cannot be ended
        is given to `on_error(service, exc)`, and the others still end."""
        for service in reversed(list(self.services.values())):
            mode = self.modes[service.id]
            if mode == KEPT:
                continue
            verb = "left alone" if service.existing else "stopping"
            _log.info("service %s (%s): %s at %s", service.id, mode, verb, service.address)
            try:
                service.end(failed)
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(service, exc)
        self.services.clear()
