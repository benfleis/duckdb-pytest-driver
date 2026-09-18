"""The one object a test body receives."""

import os

from .client import Client, SqlError
from .shell import DuckShell, last_json_array
from .steps import step


class Ctx:
    def __init__(self, item, *, temp_dir, duckdb_bin, engines):
        self.id = item.id
        self.case = item.case
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._engines = {e: engines[e] for e in item.test.engines}

    def duckdb(self, preamble="", *, table=None):
        """The built duckdb shell as a client. Every statement is a fresh process running
        `preamble + sql`, so it always sees the latest committed state.

        TODO: one process per statement does not scale (about 0.5s each on a debug build, plus a
        re-ATTACH). The target is one shell per test with statements piped in, and a full disconnect
        only where a test needs caches flushed.
        """
        shell = DuckShell(self._duckdb_bin, preamble)

        def run(sql):
            p = shell._run(sql, json_out=True)
            if p.returncode != 0:
                raise SqlError(p.stderr.strip())
            rows = last_json_array(p.stdout)
            return (list(rows[0]) if rows else []), [tuple(row.values()) for row in rows]

        return Client("duckdb", run, table=table)

    def engine(self, name, *, table=None):
        if name not in self._engines:
            raise KeyError(f"engine {name!r} is not declared by this test (engines={sorted(self._engines)})")
        return Client(name, self._engines[name].run, table=table)

    def location(self, name):
        """A path under this test's temp dir for a table the body creates. Not created here."""
        return os.path.join(self.temp_dir, name)

    step = staticmethod(step)
