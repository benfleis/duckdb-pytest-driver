"""The one object a test body receives."""

import os

from .client import Client, SetupFailed, SqlError
from .engines.duckdb import DEFAULT_TIMEOUT_S, DuckDBPerCall
from .steps import step


def _no_reconnect():
    raise NotImplementedError(
        "reconnect() is a new connection on the same instance; the duckdb shell has one connection. "
        "restart() gives a new instance, respawn() a new process."
    )


class Ctx:
    def __init__(
        self,
        case,
        *,
        temp_dir,
        duckdb_bin,
        duckdb_session,
        engines,
        duckdb_home=None,
        services=None,
        remote_parts=(),
        init_sql="",
        run_test=None,
    ):
        self.id = case.id
        self.case = case.params
        self.variant = {axis: v for axis, v in case.axis_values.items()}
        self.values = case.values
        self._settings = case.settings
        self._init_sql = init_sql
        self._run_test = run_test
        self._location_on = next((v.location_on for v in case.axis_values.values() if v.location_on), None)
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._duckdb_session = duckdb_session
        self._duckdb_home = duckdb_home
        self._engines = {e: engines[e] for e in case.engines}
        self._services = {s: services[s] for s in case.services} if services else {}
        self._remote_parts = remote_parts
        self.remote_locations = []  # (service id, uri): what this test may have written outside temp_dir
        self.failed_checks = []
        self.teardowns = []
        self.clients = []
        self.clients = []

    def duckdb(self, preamble="", *, table=None, session="per_run", timeout=DEFAULT_TIMEOUT_S):
        """DuckDB as a client. `per_run` (the rule): the run's one shell, on the instance this test
        started fresh; `preamble` runs once, now. The client has `restart()` (new instance, then the
        test's preambles again) and `respawn()` (new process, the same). `per_call`: a cold process
        for every call, running `preamble` each time.

        `timeout`, in seconds, is for each statement (`per_call`: for each call); after it the shell
        is killed and the statement has failed.

        A body that uses threads: the one shell takes one statement at a time, so threads on `per_run`
        clients are safe and wait for each other. Use `per_call` only when the statements must run
        in parallel; each call is then its own process, with nothing shared but files."""
        preamble = "\n".join(x for x in (self._variant_preamble(), preamble) if x)
        if session == "per_call":
            per_call = DuckDBPerCall(self._duckdb_bin, preamble, timeout=timeout, home=self._duckdb_home)
            return self._client("duckdb", per_call.run, table)
        if session != "per_run":
            raise ValueError(f"session={session!r}; it is 'per_run' or 'per_call'")
        shared = self._duckdb_session
        try:
            shared.add_preamble(preamble)
        except SqlError as exc:
            raise SetupFailed(f"[duckdb] the preamble failed: {exc}") from None
        client = self._client("duckdb", lambda statements: shared.run(statements, timeout=timeout), table)
        client.restart, client.respawn, client.reconnect = shared.restart, shared.respawn, _no_reconnect
        return client

    def engine(self, name, *, table=None):
        if name not in self._engines:
            raise KeyError(f"engine {name!r} is not declared by this test (engines={sorted(self._engines)})")
        return self._client(name, self._engines[name].run, table)

    def _variant_preamble(self):
        """What the case's variant values bring for DuckDB: init SQL, then settings."""
        sets = [f"SET {k} = '{str(v).replace(chr(39), chr(39) * 2)}';" for k, v in self._settings.items()]
        return "\n".join([self._init_sql, *sets]).strip()

    def run_test(self, *, env=None, init_sql=""):
        """For a paired test: run the `.test` file of this body's stem through the unittest binary.
        `env` adds `{NAME}` values for the file; `init_sql` runs before it."""
        if self._run_test is None:
            raise RuntimeError("run_test() is for a Python body that has a .test file of the same stem")
        self._run_test(env=env, init_sql=init_sql)

    def _client(self, name, run, table):
        client = Client(
            name, run, table=table, values=self.values, record=self.failed_checks.append, defer=self.teardowns.append
        )
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
        on = on or self._location_on
        if on is None:
            return os.path.join(self.temp_dir, name)
        provisioned = self._services[on] if on in self._services else self.service(on)
        uri = provisioned.service.location(provisioned.block, *self._remote_parts, name)
        self.remote_locations.append((on, uri))
        return uri

    step = staticmethod(step)
