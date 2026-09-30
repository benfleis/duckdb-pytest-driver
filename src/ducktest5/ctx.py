"""The one object a test body receives."""

import os

from .binary import UsageError
from .leases import identity, template_names
from .resources.client import Env, SetupFailed, SqlError
from .steps import step


class Context:
    """What a test body receives: the Test's own binder. A Client on each engine the test holds
    (`client(name)`), its `env`, its `params` row, and what it gathers as it runs -- the checks that
    failed, the teardowns, the log of its statements. Its identity is the Test's: the engine
    instances it holds and its test environment.

    `env` holds the `{NAME}` values its templates see, by the names a `.test` sees them: `temp_dir`
    (the test's own place), `temp_dir_root` (the run level above it), `data_dir`, `local_temp_dir`,
    `local_data_dir`, each axis name (the id of the test's value on it), and the test's values."""

    def __init__(
        self,
        test,
        test_env,
        engines=(),
        *,
        temp_dir=None,
        services=None,
        run_paired=None,
    ):
        self.id = test.id
        self.params = test.params
        self._test_env = test_env
        self.env = Env(template_names(test_env, temp_dir))
        self.temp_dir = temp_dir
        self.failed_checks, self.teardowns, self.statements = [], [], []  # statements: {client, sql, seconds}
        self.clients = {}  # engine id -> the Client on the instance this test holds
        self._own = []  # the clients `client(name, new=True)` made
        for engine in engines:
            self.clients[engine.id] = engine.client(**self._wiring())
        self._body_path = test.definition.path
        self._run_paired = run_paired
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

    def client(self, name, *, new=False):
        """The Client on the engine `name`: the SQL vocabulary of `Client`, on the instance this test
        holds, reset and given its preamble before the body began. It sees `ctx.env`; what is set
        on its own `env` reaches its templates alone.

        `new=True` gives another Client of the same engine, on a handle of its own (DuckDB: a shell
        process of its own), with the same preamble and process environment, closed when the test
        ends. A client takes one statement at a time, so threads that share one wait for each
        other; statements that must run at the same time need a client each."""
        if name not in self.clients:
            raise UsageError(
                f"ctx.client({name!r}): this test holds engines {sorted(self.clients)}; "
                f"add {name!r} to @test(engines=[...])"
            )
        if not new:
            return self.clients[name]
        engine = self.clients[name].engine.twin()
        self.teardowns.append(engine.close)
        try:
            engine.reset()
        except SqlError as exc:
            raise SetupFailed(f"[{name}] the preamble failed: {exc}") from None
        client = engine.client(**self._wiring())
        self._own.append(client)
        return client

    def run_paired(self, *, env=None, init_sql=""):
        """Run the `.test` file of this body's stem, beside it, through the unittest binary. `env`
        adds `{NAME}` values for the file; `init_sql` runs before it."""
        if self._run_paired is None:
            sibling = os.path.splitext(self._body_path)[0] + ".test"
            raise UsageError(
                f"ctx.run_paired() runs the .test file of the body's stem, and there is no {sibling} "
                f"beside {self._body_path}"
            )
        self._run_paired(env=env, init_sql=init_sql)

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
        """`{TEMP_DIR}/name`: a place for a table the body creates; nothing is created here."""
        return os.path.join(self.env.temp_dir, name)

    step = staticmethod(step)
