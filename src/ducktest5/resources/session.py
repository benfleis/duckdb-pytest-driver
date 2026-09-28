"""Sessions: a live engine handle that takes a test's inputs and holds them for its scope.

A Session serves one test at a time. A Session wider than one test is reset between tests: a fresh
instance, then its preamble again. After a test that failed it is closed and opened again instead,
so nothing a broken test left behind can reach the next one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Callable

from ..binary import UsageError
from .base import EXCLUSIVE, RW, Resource, ResourceDef
from .dialect import Preamble, preamble

TEST, BATCH, WORKER = "test", "batch", "worker"
SCOPES = (TEST, BATCH, WORKER)


@dataclass(frozen=True)
class Opener:
    """How a Session gets its live handle: `open(definition)` returns it, and `dialect` names how the
    Session's preamble is written for it (None: the engine takes no preamble)."""

    dialect: str | None
    open: Callable


@dataclass(eq=False)
class SessionDef(ResourceDef):
    """An engine a test drives. `engine` is `duckdb` (a DuckDB shell) or the id of one of ENGINES.
    `requires` and `settings` are loaded and set before every test; `init` are statements that
    follow them, and `env` sets (a value) or unsets (None) process variables of the engine.

    `scope` says how long one instance lives: `test`, `batch` or `worker`. A Python body is a batch
    of its own, so for it `batch` is `test`. Two tests whose Sessions have the same identity may
    share one instance, one after the other."""

    kind = "session"
    sharing = EXCLUSIVE

    id: str
    engine: str = "duckdb"
    requires: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    scope: str = WORKER
    timeout: float | None = None
    init: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    inputs: list = field(default_factory=list)
    retain: str = "never"

    not_content = ResourceDef.not_content + ("scope", "timeout")

    def __post_init__(self):
        if self.scope not in SCOPES:
            raise UsageError(f"session {self.id!r}: scope {self.scope!r}; it is one of {SCOPES}")

    def with_(self, **changes) -> SessionDef:
        return replace(self, **changes)

    def make(self, inputs=None, *, run_token=None, mode=RW, opener: Opener | None = None) -> Session:
        session = super().make(inputs, run_token=run_token, mode=mode)
        session.opener = opener
        return session


class Session(Resource):
    """The live instance. `live` is the engine handle while it is open, else None."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        self.opener: Opener | None = None
        self.live = None

    @property
    def dialect(self) -> str | None:
        return self.opener.dialect if self.opener else None

    def start(self):
        if self.live is not None:
            return
        for resource in self.inputs.values():
            resource.create()
        self.live = self.opener.open(self.definition)

    def stop(self):
        live, self.live = self.live, None
        if live is not None and hasattr(live, "close"):
            live.close()

    close = stop

    def reopen(self):
        self.close()
        self.start()

    def reset(self):
        """A fresh instance on the open handle, then the preamble again."""
        self.live.begin_test()
        preamble = self.preamble()
        for key, value in preamble.conf.items():
            self.live.set_conf(key, value)
        if preamble.sql:
            self.live.add_preamble("\n".join(preamble.sql))

    def preamble(self):
        """What the Session applies before each test, in its dialect. Secrets are read now."""
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


SessionDef.instance = Session


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


def duckdb_session(id="duckdb", **fields) -> SessionDef:
    return SessionDef(id, engine="duckdb", **fields)


def spark_session(id="spark", *, engine="spark", **fields) -> SessionDef:
    return SessionDef(id, engine=engine, **fields)
