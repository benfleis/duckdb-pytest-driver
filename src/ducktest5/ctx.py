"""The one object a test body receives."""

import os

from .client import Client
from .engines.duckdb import DuckDBPerCall
from .steps import step


def _no_reconnect():
    raise NotImplementedError(
        "reconnect() is a new connection on the same instance; the duckdb shell has one connection. "
        "restart() gives a new instance, respawn() a new process."
    )


class Ctx:
    def __init__(self, item, *, temp_dir, duckdb_bin, duckdb_session, engines):
        self.id = item.id
        self.case = item.case
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._duckdb_session = duckdb_session
        self._engines = {e: engines[e] for e in item.test.engines}
        self.failed_checks = []
        self.teardowns = []
        self.clients = []
        self.clients = []

    def duckdb(self, preamble="", *, table=None, session="per_run"):
        """DuckDB as a client. `per_run` (the rule): the run's one shell, on the instance this test
        started fresh; `preamble` runs once, now. The client has `restart()` (new instance, then the
        test's preambles again) and `respawn()` (new process, the same). `per_call`: a cold process
        for every call, running `preamble` each time."""
        if session == "per_call":
            return self._client("duckdb", DuckDBPerCall(self._duckdb_bin, preamble).run, table)
        if session != "per_run":
            raise ValueError(f"session={session!r}; it is 'per_run' or 'per_call'")
        shared = self._duckdb_session
        shared.add_preamble(preamble)
        client = self._client("duckdb", shared.run, table)
        client.restart, client.respawn, client.reconnect = shared.restart, shared.respawn, _no_reconnect
        return client

    def engine(self, name, *, table=None):
        if name not in self._engines:
            raise KeyError(f"engine {name!r} is not declared by this test (engines={sorted(self._engines)})")
        return self._client(name, self._engines[name].run, table)

    def _client(self, name, run, table):
        client = Client(name, run, table=table, record=self.failed_checks.append, defer=self.teardowns.append)
        self.clients.append(client)
        return client

    def seconds_by_client(self):
        totals = {}
        for client in self.clients:
            totals[client.name] = totals.get(client.name, 0.0) + client.seconds
        return {name: round(seconds, 2) for name, seconds in totals.items()}

    def location(self, name):
        """A path under this test's temp dir for a table the body creates. Not created here."""
        return os.path.join(self.temp_dir, name)

    step = staticmethod(step)
