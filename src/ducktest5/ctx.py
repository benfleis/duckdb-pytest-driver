"""The one object a test body receives."""

import os

from .client import Client
from .engines.duckdb import DuckDBShell
from .steps import step


class Ctx:
    def __init__(self, item, *, temp_dir, duckdb_bin, engines):
        self.id = item.id
        self.case = item.case
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._engines = {e: engines[e] for e in item.test.engines}

    def duckdb(self, preamble="", *, table=None):
        """The built duckdb shell as a client. Every call is a fresh process running `preamble` then
        the call's statements, so it always sees the latest committed state.

        TODO: one process per call still costs about 0.5s on a debug build, plus the re-ATTACH. The
        target is one shell per test, with a full disconnect only where a test needs caches flushed.
        """
        return Client("duckdb", DuckDBShell(self._duckdb_bin, preamble).run, table=table)

    def engine(self, name, *, table=None):
        if name not in self._engines:
            raise KeyError(f"engine {name!r} is not declared by this test (engines={sorted(self._engines)})")
        return Client(name, self._engines[name].run, table=table)

    def location(self, name):
        """A path under this test's temp dir for a table the body creates. Not created here."""
        return os.path.join(self.temp_dir, name)

    step = staticmethod(step)
