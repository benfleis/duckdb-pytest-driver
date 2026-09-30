"""Engines: a service a test drives with SQL, declared among SERVICES. The run does not provision
one as it does the other services: a test that names it leases an instance, and gets a Client on it.

An instance holds a live SQL handle -- a DuckDB shell, a Spark session -- and serves one test at a
time. An instance wider than one test is reset between tests: a fresh instance on the handle, then
its preamble again. After a test that failed it is closed and opened again instead, so nothing a
broken test left behind can reach the next one.

`scope` says how long one instance lives: `worker` (one per worker and identity, warm from test to
test), `batch` (one per batch; a Python body is a batch of its own) or `run` (one for the run, held
by one test at a time). Two tests whose engines have one identity may share one instance, one after
the other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Callable

from ..binary import UsageError
from .base import EXCLUSIVE, NEVER, RW
from .client import Client
from .dialect import Preamble, preamble
from .service import Service, ServiceDef

WORKER, BATCH, RUN = "worker", "batch", "run"
SCOPES = (WORKER, BATCH, RUN)


@dataclass
class Runtime:
    """What the run gives an engine instance to open its handle with."""

    shell: Callable | None = None  # process env or None -> a DuckDB shell
    booted: dict = field(default_factory=dict)  # engine id -> the handle its boot returned


@dataclass(eq=False)
class EngineDef(ServiceDef):
    """An engine. `requires` and `settings` are loaded and set before every test; `init` are
    statements that follow them, and `env` sets (a value) or unsets (None) process variables of the
    engine. They are part of its identity; `scope` and `timeout` (for one statement, in seconds) are
    not.

    A kind is a dataclass with these fields, then its own; it has a `dialect`, the way its preamble
    and the storages and credentials among its inputs are written for it (`resources/dialect.py`;
    None: it takes no inputs), and `open(runtime)`, which returns a live handle with
    `run(statements)`, `begin_test()` and `close()`."""

    kind = "engine"
    sharing = EXCLUSIVE
    provisioned = False
    dialect = None  # a class attribute of a kind; not annotated, so a kind may make it a field

    id: str
    requires: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    init: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    scope: str = WORKER
    timeout: float | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = NEVER

    not_content = ServiceDef.not_content + ("scope", "timeout")

    def __post_init__(self):
        if self.scope not in SCOPES:
            raise UsageError(f"engine {self.id!r}: scope {self.scope!r}; it is one of {SCOPES}")

    def with_(self, **changes) -> EngineDef:
        return replace(self, **changes)

    def open(self, runtime: Runtime):
        raise NotImplementedError(f"engine kind {self.kind!r} has no open()")


class Engine(Service):
    """The live instance. `live` is the handle while it is open, else None; `runtime` is what the
    run gave it to open one."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        self.runtime: Runtime | None = None
        self.live = None

    @property
    def dialect(self) -> str | None:
        return self.definition.dialect

    @property
    def owned(self):
        return False  # its handle ends with the run's process: nothing to sweep

    def start(self):
        if self.live is not None:
            return
        for resource in self.inputs.values():
            resource.create()
        self.live = self.definition.open(self.runtime)

    def stop(self):
        live, self.live = self.live, None
        if live is not None and hasattr(live, "close"):
            live.close()

    close = stop

    def create(self):
        self.start()

    def delete(self):
        self.stop()

    def reopen(self):
        self.close()
        self.start()

    def reset(self):
        """A fresh instance on the open handle, then the preamble again."""
        self.live.begin_test()
        given = self.preamble()
        for key, value in given.conf.items():
            self.live.set_conf(key, value)
        if given.sql:
            self.live.add_preamble("\n".join(given.sql))

    def preamble(self) -> Preamble:
        """What the engine applies before each test, in its dialect. Secrets are read now."""
        if self.dialect is None:
            return Preamble()
        return preamble(self.dialect, self.definition, self.inputs.values())

    def ready(self) -> bool:
        return self.live is not None

    def read(self):
        return {"id": self.id, "identity": self.identity, "open": self.live is not None}

    def acquire(self, mode=RW, key=None):
        """Held by one test: opened if it is closed, then reset."""
        held = super().acquire(mode, key)
        try:
            self.start()
            self.reset()
        except BaseException:
            super().release(held, mode)
            raise
        return held

    def release(self, held, mode=RW, *, failed=False):
        if failed:
            self.close()
        super().release(held, mode)

    def run(self, statements, *, timeout=None):
        return self.live.run(statements)

    def client(self, name=None, *, timeout=None, **fields) -> Client:
        """A Client on this instance for the test that holds it: the SQL verbs, run here."""
        return Client(name or self.id, lambda statements: self.run(statements, timeout=timeout), engine=self, **fields)


EngineDef.instance = Engine


@dataclass(eq=False)
class BootedEngineDef(EngineDef):
    """An engine booted once per run, such as a JVM. `boot(log_path)` returns the run's handle, with
    `run(statements)` and `stop()`; each instance opens on it, a handle of its own where the run's
    has `for_worker()` (a Spark clone). `version` names what was booted (e.g. "spark:4.2:delta-4.4")
    for the scan record and the report. The booted engine's `dialect` is given here."""

    kind = "booted"

    version: str = ""
    boot: Callable | None = None
    dialect: str | None = None

    def open(self, runtime):
        handle = runtime.booted[self.id]
        return handle.for_worker() if hasattr(handle, "for_worker") else handle


def process_env(env: dict) -> dict | None:
    """The process environment an engine with `env` gets; None when it is this process's."""
    if not env:
        return None
    out = dict(os.environ)
    for name, value in env.items():
        value = value() if callable(value) else value
        if value is None:
            out.pop(name, None)
        else:
            out[name] = str(value)
    return out
