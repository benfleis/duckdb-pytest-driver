"""The one object a test body receives."""

import os

from .client import Client, SetupFailed, SqlError
from .engines.duckdb import DEFAULT_TIMEOUT_S, DuckDBPerCall, DuckDBSession
from .resources.session import process_env
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
        sessions,
        test_env,
        duckdb_home=None,
        services=None,
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
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._sessions = sessions  # id -> the Session this test holds
        self._duck = next(s for s in sessions.values() if s.definition.engine == "duckdb")
        self._duckdb_home = duckdb_home
        self._session_factory = session_factory
        self._engines = list(test.engines)
        self._services = dict(services or {})  # id -> the run's instance of each service behind its inputs
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
        HOME and the same test environment: the preamble of the test's DuckDB Session (the
        extensions loaded, the settings set, the scoped secrets created, then the init SQL) and its
        process variables.

        `timeout`, in seconds, is for each statement (`per_call`: for each call); after it the shell
        is killed and the statement has failed.

        A body that uses threads: a shell takes one statement at a time, so threads that share a
        `per_run` client are safe and wait for each other. For statements that must really run at the
        same time, give each thread a client of its own: `per_client` is a warm shell each, `per_call`
        a cold process per call, with nothing shared but files."""
        if session not in SESSIONS:
            raise ValueError(f"session={session!r}; it is one of {SESSIONS}")
        shell = self._duck.live
        if session != "per_run":
            preamble = "\n".join(x for x in (*self._duck.preamble().sql, preamble) if x)
            environ = process_env(self._duck.definition.env)
        if session == "per_call":
            per_call = DuckDBPerCall(self._duckdb_bin, preamble, timeout=timeout, home=self._duckdb_home, env=environ)
            return self._client("duckdb", per_call.run, table)
        if session == "per_client":
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

    def session(self, name, *, table=None):
        """A client on the Session `name` this test holds: the SQL vocabulary of `Client`, on the
        engine that Session opened, reset and given its preamble before the body began. `.session`
        is the Session itself."""
        if name not in self._sessions:
            raise KeyError(f"session {name!r} is not one this test holds (sessions={sorted(self._sessions)})")
        held = self._sessions[name]
        if held.definition.engine == "duckdb":
            timeout = held.definition.timeout or DEFAULT_TIMEOUT_S
            client = self._client(name, lambda statements: held.live.run(statements, timeout=timeout), table)
        else:
            client = self._client(name, lambda statements: held.live.run(statements), table)
        client.session = held
        return client

    def engine(self, name, *, table=None):
        """A client on the engine `name`, through the Session this test holds on it."""
        if name not in self._engines:
            raise KeyError(f"engine {name!r} is not declared by this test (engines={sorted(self._engines)})")
        held = next(s for s in self._sessions.values() if s.definition.engine == name)
        return self._client(name, lambda statements: held.live.run(statements), table)

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
        """The run's instance of a service behind this test's inputs; `.attributes` are what it
        gives, never its secrets."""
        if name not in self._services:
            raise KeyError(f"service {name!r} is not behind this test's inputs (services={sorted(self._services)})")
        return self._services[name]

    def location(self, name):
        """A place for a table the body creates; nothing is created here. A path under this test's
        temp dir, or, where a bound storage or a variant gives a `temp_dir_root`, under this test's
        own place there, which is under the run's."""
        root = self.roots.get("temp_dir_root")
        return os.path.join(root or self.temp_dir, name)

    step = staticmethod(step)
