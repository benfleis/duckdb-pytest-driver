"""What one run made: every instance carries the run's `run_token`, and each service is made once
and shared by every consumer."""

from __future__ import annotations

from typing import Iterable

from .base import RW, Resource, ResourceDef
from .service import Service, ServiceDef, ServiceNotUp


def services_in(definitions: Iterable[ResourceDef]) -> list[ServiceDef]:
    """The services among `definitions` and their inputs, inputs first, each once."""
    found = {}

    def walk(d):
        for _, _, sub in d.edges():
            if sub is not None:
                walk(sub)
        if isinstance(d, ServiceDef):
            found.setdefault(d.id, d)

    for d in definitions:
        walk(d)
    return list(found.values())


class Made:
    """The run's instances. `run_token` is None where nothing is made for a run (scan, plan): a
    service is then never up, and what needs one waits for the run."""

    def __init__(self, run_token: str | None = None):
        self.run_token = run_token
        self.services: dict[str, Service] = {}
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

    def create(self, definition: ServiceDef) -> Service:
        """Start the service, or adopt the existing one; raises ServiceNotUp, and remembers why."""
        try:
            service = definition.make(self.inputs_of(definition), run_token=self.run_token)
            service.create()
        except Exception as exc:
            self.errors[definition.id] = str(exc) if isinstance(exc, ServiceNotUp) else f"{type(exc).__name__}: {exc}"
            raise ServiceNotUp(self.errors[definition.id]) from exc
        self.services[definition.id] = service
        return service

    def end(self, failed: bool):
        """Every service ends, the last one made first."""
        for service in reversed(list(self.services.values())):
            service.end(failed)
        self.services.clear()
