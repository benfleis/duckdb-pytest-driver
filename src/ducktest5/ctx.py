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
    def __init__(self, item, *, temp_dir, duckdb_bin, duckdb_session, engines, services=None, remote_parts=()):
        self.id = item.id
        self.case = item.case
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._duckdb_session = duckdb_session
        self._engines = {e: engines[e] for e in item.test.engines}
        self._services = {s: services[s] for s in item.test.services} if services else {}
        self._remote_parts = remote_parts
        self.remote_locations = []  # (service id, uri): what this test may have written outside temp_dir
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

    def service(self, name):
        """The facts of a service this test declared: endpoint, bucket, credentials."""
        if name not in self._services:
            raise KeyError(f"service {name!r} is not declared by this test (services={sorted(self._services)})")
        return self._services[name].block

    def location(self, name, *, on=None):
        """A place for a table the body creates; nothing is created here. By default a path under
        this test's temp dir; with `on=<service>`, a URI under this run's prefix on that service."""
        if on is None:
            return os.path.join(self.temp_dir, name)
        provisioned = self._services[on] if on in self._services else self.service(on)
        uri = provisioned.service.location(provisioned.block, *self._remote_parts, name)
        self.remote_locations.append((on, uri))
        return uri

    step = staticmethod(step)
