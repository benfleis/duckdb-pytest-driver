"""The one object a test body receives."""

import os

from .client import Client, SetupFailed, SqlError
from .engines.duckdb import DEFAULT_TIMEOUT_S, DuckDBPerCall, DuckDBSession
from .steps import step

SESSIONS = ("per_run", "per_client", "per_call")


def _no_reconnect():
    raise NotImplementedError(
        "reconnect() is a new connection on the same instance; the duckdb shell has one connection. "
        "restart() gives a new instance, respawn() a new process."
    )


class Ctx:
    def __init__(
        self,
        test,
        *,
        temp_dir,
        duckdb_bin,
        duckdb_session,
        engines,
        test_env,
        duckdb_home=None,
        services=None,
        remote_parts=(),
        run_test=None,
        session_factory=DuckDBSession,
    ):
        self.id = test.id
        self.params = test.params
        self.variant = {axis: v for axis, v in test.axis_values.items()}
        self.environment = test_env
        self.values = test_env.value_map
        self.roots = test_env.root_paths
        self._run_test = run_test
        self._location_on = next((v.location_on for v in test.axis_values.values() if v.location_on), None)
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._duckdb_session = duckdb_session
        self._duckdb_home = duckdb_home
        self._session_factory = session_factory
        self._engines = {e: engines[e] for e in test.engines}
        self._services = {s: services[s] for s in test.services} if services else {}
        self._remote_parts = remote_parts
        self.remote_locations = []  # (service id, uri): what this test may have written outside temp_dir
        self.failed_checks = []
        self.teardowns = []
        self.clients = []
        self.statements = []  # {client, sql, seconds} per call, for a verbose report

    def duckdb(self, preamble="", *, table=None, session="per_run", timeout=DEFAULT_TIMEOUT_S):
        """DuckDB as a client. `per_run` (the rule): the shell of the worker that runs this test, on
        the instance this test started fresh; `preamble` runs once, now. The client has `restart()`
        (new instance, then the test's preambles again) and `respawn()` (new process, the same).
        `per_client`: the same, on a shell of this client's own, closed when the test ends. `per_call`:
        a cold process for every call, running `preamble` each time. All three get the same private
        HOME and the same test
        environment: the extensions loaded, the settings set, then the init SQL.

        A test whose environment sets or unsets process variables gets a shell of its own even when
        `per_run` was asked for: the run's one shell was started before the test was known and
        cannot change its environment.

        `timeout`, in seconds, is for each statement (`per_call`: for each call); after it the shell
        is killed and the statement has failed.

        A body that uses threads: a shell takes one statement at a time, so threads that share a
        `per_run` client are safe and wait for each other. For statements that must really run at the
        same time, give each thread a client of its own: `per_client` is a warm shell each, `per_call`
        a cold process per call, with nothing shared but files."""
        if session not in SESSIONS:
            raise ValueError(f"session={session!r}; it is one of {SESSIONS}")
        preamble = "\n".join(x for x in (self._test_preamble(), preamble) if x)
        environ = self.environment.environ()
        if session == "per_call":
            per_call = DuckDBPerCall(self._duckdb_bin, preamble, timeout=timeout, home=self._duckdb_home, env=environ)
            return self._client("duckdb", per_call.run, table)
        shell = self._duckdb_session
        if session == "per_client" or environ is not None:
            shell = self._session_factory(self._duckdb_bin, home=self._duckdb_home, env=environ)
            shell.begin_test()
            self.teardowns.append(shell.close)
        try:
            shell.add_preamble(preamble)
        except SqlError as exc:
            raise SetupFailed(f"[duckdb] the preamble failed: {exc}") from None
        client = self._client("duckdb", lambda statements: shell.run(statements, timeout=timeout), table)
        client.restart, client.respawn, client.reconnect = shell.restart, shell.respawn, _no_reconnect
        return client

    def engine(self, name, *, table=None):
        if name not in self._engines:
            raise KeyError(f"engine {name!r} is not declared by this test (engines={sorted(self._engines)})")
        return self._client(name, self._engines[name].run, table)

    def _test_preamble(self):
        """The test environment as SQL, in the order the `.test` init file has it."""
        return "\n".join([f"LOAD {x};" for x in self.environment.extensions] + [self.environment.sql]).strip()

    def run_test(self, *, env=None, init_sql=""):
        """For a paired test: run the `.test` file of this body's stem through the unittest binary.
        `env` adds `{NAME}` values for the file; `init_sql` runs before it."""
        if self._run_test is None:
            raise RuntimeError("run_test() is for a Python body that has a .test file of the same stem")
        self._run_test(env=env, init_sql=init_sql)

    def _client(self, name, run, table):
        client = Client(
            name,
            run,
            table=table,
            values=self.values,
            record=self.failed_checks.append,
            defer=self.teardowns.append,
            log=self.statements.append,
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
        this test's temp dir, or under the test environment's `temp_dir_root` where a variant gives
        one; with `on=<service>`, a URI under this run's prefix on that service."""
        on = on or self._location_on
        if on is None:
            root = self.roots.get("temp_dir_root")
            return os.path.join(root, *self._remote_parts, name) if root else os.path.join(self.temp_dir, name)
        provisioned = self._services[on] if on in self._services else self.service(on)
        uri = provisioned.service.location(provisioned.block, *self._remote_parts, name)
        self.remote_locations.append((on, uri))
        return uri

    step = staticmethod(step)
