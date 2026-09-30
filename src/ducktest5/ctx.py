"""The one object a test body receives."""

import os

from .engines.duckdb import DEFAULT_TIMEOUT_S, DuckDBPerCall, DuckDBSession
from .resources.client import Names, SetupFailed, SqlError
from .resources.engine import process_env
from .steps import step

SESSIONS = ("per_run", "per_client", "per_call")


def _no_reconnect():
    raise NotImplementedError(
        "reconnect() is a new connection on the same instance; the duckdb shell has one connection. "
        "restart() gives a new instance, respawn() a new process."
    )


class Ctx:
    """`session` is the test's Session: a Client on each engine it leases (`client(name)`), and
    what the test gathers as it runs."""

    def __init__(
        self,
        test,
        *,
        temp_dir,
        duckdb_bin,
        session,
        duckdb_home=None,
        services=None,
        run_test=None,
        session_factory=DuckDBSession,
    ):
        self.id = test.id
        self.params = test.params
        self.variant = {axis: v for axis, v in test.axis_values.items()}
        self.session = session
        self.environment = session.environment
        self.values = Names(self.environment.value_map)
        self.roots = self.environment.root_paths
        self._run_test = run_test
        self.temp_dir = temp_dir
        self._duckdb_bin = duckdb_bin
        self._duckdb_home = duckdb_home
        self._session_factory = session_factory
        self._services = dict(services or {})  # id -> the run's instance of each service behind its inputs

    @property
    def failed_checks(self):
        return self.session.failed_checks

    @property
    def teardowns(self):
        return self.session.teardowns

    @property
    def statements(self):
        """{client, sql, seconds} per call, for a verbose report."""
        return self.session.statements

    def client(self, name, *, table=None, **values):
        """The Client on the engine `name`: the SQL vocabulary of `Client`, on the instance this test
        leases, reset and given its preamble before the body began. `table` is its `{t}`; `values`
        reach its templates alone -- `ctx.session.bind(...)` gives a value to every Client."""
        return self.session.client(name, table=table, **values)

    def duckdb(self, preamble="", *, table=None, session="per_run", timeout=DEFAULT_TIMEOUT_S):
        """DuckDB as a client. `per_run` (the rule): the shell of the test's DuckDB engine, the one
        `client()` also runs on, on the instance this test started fresh; `preamble` runs once, now.
        The client has `restart()` (new instance, then the test's preambles again) and `respawn()`
        (new process, the same).
        `per_client`: the same, on a shell of this client's own, closed when the test ends. `per_call`:
        a cold process for every call, running `preamble` each time. All three get the same private
        HOME and the same test environment: the preamble of the test's DuckDB engine (the
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
        engine = self.session.duckdb.engine
        shell = engine.live
        if session != "per_run":
            preamble = "\n".join(x for x in (*engine.preamble().sql, preamble) if x)
            environ = process_env(engine.definition.env)
        if session == "per_call":
            per_call = DuckDBPerCall(self._duckdb_bin, preamble, timeout=timeout, home=self._duckdb_home, env=environ)
            return self.session.own_client("duckdb", per_call.run, table=table)
        if session == "per_client":
            shell = self._session_factory(self._duckdb_bin, home=self._duckdb_home, env=environ)
            shell.begin_test()
            self.teardowns.append(shell.close)
        try:
            shell.add_preamble(preamble)
        except SqlError as exc:
            raise SetupFailed(f"[duckdb] the preamble failed: {exc}") from None
        client = self.session.own_client(
            "duckdb", lambda statements: shell.run(statements, timeout=timeout), table=table, engine=engine
        )
        client.restart, client.respawn, client.reconnect = shell.restart, shell.respawn, _no_reconnect
        return client

    def run_test(self, *, env=None, init_sql=""):
        """For a paired test: run the `.test` file of this body's stem through the unittest binary.
        `env` adds `{NAME}` values for the file; `init_sql` runs before it."""
        if self._run_test is None:
            raise RuntimeError("run_test() is for a Python body that has a .test file of the same stem")
        self._run_test(env=env, init_sql=init_sql)

    def seconds_by_client(self):
        return self.session.seconds_by_client()

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
