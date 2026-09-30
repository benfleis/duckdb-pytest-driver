"""The one object a test body receives."""

import os

from .binary import UsageError
from .engines.duckdb import DEFAULT_TIMEOUT_S, DuckDBPerCall, DuckDBShell
from .leases import DEFAULT, identity, template_names
from .resources.client import Client, Env, SetupFailed, SqlError
from .resources.engine import environ
from .steps import step

SHELLS = ("per_run", "per_client", "per_call")


def _no_reconnect():
    raise NotImplementedError(
        "reconnect() is a new connection on the same instance; the duckdb shell has one connection. "
        "restart() gives a new instance, respawn() a new process."
    )


class Context:
    """What a test body receives: the Test's own binder. A Client on each engine the test holds
    (`client(name)`), its `env` (the `{NAME}` values its templates see), its `params` row, and what
    it gathers as it runs -- the checks that failed, the teardowns, the log of its statements. Its
    identity is the Test's: the engine instances it holds and its test environment."""

    def __init__(
        self,
        test,
        test_env,
        engines=(),
        *,
        temp_dir=None,
        duckdb_bin=None,
        duckdb_home=None,
        services=None,
        run_test=None,
        shell_factory=DuckDBShell,
    ):
        self.id = test.id
        self.params = test.params
        self.variant = {axis: v for axis, v in test.axis_values.items()}
        self._test_env = test_env
        self.env = Env(template_names(test_env, temp_dir))
        self.roots = test_env.root_paths
        self.temp_dir = temp_dir
        self.failed_checks, self.teardowns, self.statements = [], [], []  # statements: {client, sql, seconds}
        self.clients = {}  # engine id -> the Client on the instance this test holds
        self._own = []  # clients on no lease: a shell a body starts for itself
        for engine in engines:
            self.clients[engine.id] = engine.client(**self._wiring())
        self._run_test = run_test
        self._duckdb_bin = duckdb_bin
        self._duckdb_home = duckdb_home
        self._shell_factory = shell_factory
        self._services = dict(services or {})  # id -> the run's instance of each service behind its inputs

    def _wiring(self):
        return {
            "env": self.env,
            "record": self.failed_checks.append,
            "defer": self.teardowns.append,
            "log": self.statements.append,
        }

    @property
    def identity(self) -> str:
        return identity([c.engine.identity for c in self.clients.values()], self._test_env)

    def client(self, name):
        """The Client on the engine `name`: the SQL vocabulary of `Client`, on the instance this test
        holds, reset and given its preamble before the body began. It sees `ctx.env`; what is set
        on its own `env` reaches its templates alone."""
        if name not in self.clients:
            raise KeyError(f"engine {name!r} is not one this test leases; it leases {sorted(self.clients)}")
        return self.clients[name]

    def _own_client(self, name, run, *, engine=None):
        """A Client beside the leases: on a shell the body started for itself, or on a leased
        `engine` with a run of its own; logged with the rest."""
        client = Client(name, run, engine=engine, **self._wiring())
        self._own.append(client)
        return client

    @property
    def _duckdb(self):
        """The Client on the test's DuckDB engine."""
        return next(c for c in self.clients.values() if c.engine.definition.kind == DEFAULT.kind)

    def duckdb(self, preamble="", *, shell="per_run", timeout=DEFAULT_TIMEOUT_S, **gone):
        """DuckDB as a client, on the shell `shell` names. `per_run` (the rule): the shell of the test's
        DuckDB engine, the one
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
        if "session" in gone:
            raise UsageError("ctx.duckdb(session=...) is now ctx.duckdb(shell=...), with the same values")
        if gone:
            raise TypeError(f"duckdb() got unexpected keyword arguments {sorted(gone)}")
        if shell not in SHELLS:
            raise ValueError(f"shell={shell!r}; it is one of {SHELLS}")
        engine = self._duckdb.engine
        live = engine.live
        if shell != "per_run":
            preamble = "\n".join(x for x in (*engine.preamble().sql, preamble) if x)
            process = environ(engine.definition.process_env)
        if shell == "per_call":
            per_call = DuckDBPerCall(self._duckdb_bin, preamble, timeout=timeout, home=self._duckdb_home, env=process)
            return self._own_client("duckdb", per_call.run)
        if shell == "per_client":
            live = self._shell_factory(self._duckdb_bin, home=self._duckdb_home, env=process)
            live.begin_test()
            self.teardowns.append(live.close)
        try:
            live.add_preamble(preamble)
        except SqlError as exc:
            raise SetupFailed(f"[duckdb] the preamble failed: {exc}") from None
        client = self._own_client("duckdb", lambda statements: live.run(statements, timeout=timeout), engine=engine)
        client.restart, client.respawn, client.reconnect = live.restart, live.respawn, _no_reconnect
        return client

    def run_test(self, *, env=None, init_sql=""):
        """For a paired test: run the `.test` file of this body's stem through the unittest binary.
        `env` adds `{NAME}` values for the file; `init_sql` runs before it."""
        if self._run_test is None:
            raise RuntimeError("run_test() is for a Python body that has a .test file of the same stem")
        self._run_test(env=env, init_sql=init_sql)

    def seconds_by_client(self):
        totals = {}
        for client in [*self.clients.values(), *self._own]:
            totals[client.name] = totals.get(client.name, 0.0) + client.seconds
        return {name: round(seconds, 2) for name, seconds in totals.items()}

    def service(self, name):
        """The run's instance of a service behind this test's inputs; `.attribute_values()` are what
        it made, never its secrets."""
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
