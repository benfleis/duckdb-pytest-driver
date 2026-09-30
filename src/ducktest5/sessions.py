"""The Session of one test: a Client on each engine it names, and what the test gathers as it runs.

A test names engines on `@test(engines=...)`, by a rule or by a variant; each gets a Client, and one
that names no DuckDB engine gets a Client on the default one. Each engine is bound to what the test gets:
its storage and credential inputs, and, for a DuckDB engine, the test environment's requires, init
SQL and process variables. What it is bound to is part of the engine's identity, so a test with
other inputs leases another instance.

A `.test` body runs in one DuckDB engine, rendered in the `unittest` dialect: the process of its
batch. `environment()` puts that engine over the test environment, and `batch_session()` is the
Session of the result: one Client, on the `unittest` engine of a batch given that environment. Its
identity is the batch key, so two `.test` bodies share a batch exactly when their DuckDB engines and
test environments are equal. What the body itself says, an inline `require` among it, is not part of
that.
"""

from __future__ import annotations

from dataclasses import replace

from .binary import UsageError
from .engines.duckdb import duckdb_engine
from .engines.unittest import UnittestEngineDef
from .model import SQLLOGIC
from .provision import MASK, SECRET_NAME
from .resources.base import digest
from .resources.client import Client, Names
from .resources.made import planned_identity
from .testenv import Entry, set_entry

DEFAULT = duckdb_engine("duckdb")


def named(test, project) -> list:
    """The project's engines the test names, on `@test`, by a rule or by a variant."""
    return [project.engines[name] for name in test.engines] if project else []


def duckdb_of(test, project):
    """The DuckDB engine of a test: the one it names, else the default."""
    duckdb = [d for d in named(test, project) if d.kind == DEFAULT.kind]
    return duckdb[0] if duckdb else DEFAULT


def check(definition, engines):
    """A `.test` body runs in one DuckDB engine, and that engine takes the test's inputs only."""
    if definition.type != SQLLOGIC:
        return
    where = f"{definition.path}::{definition.name}"
    other = [d.id for d in engines if d.kind != DEFAULT.kind]
    if other:
        raise UsageError(f"{where} names engines {other}; a .test body runs on a DuckDB engine only")
    if len(engines) > 1:
        raise UsageError(f"{where} names engines {[d.id for d in engines]}; a .test body runs on one engine")
    if engines and engines[0].inputs:
        raise UsageError(
            f"{where} names engine {engines[0].id!r}, which has inputs of its own; bind them on the test instead"
        )


def environment(test, test_env, project):
    """`test_env` under the test's DuckDB engine, as the process of a `.test` batch is given it: the
    engine's requires, settings, init statements and process variables first. What `test_env`
    already gives is not given again, and where both set one name, the test's own is the more
    specific and wins."""
    d = duckdb_of(test, project)
    where = f"engine {d.id}"
    loaded = test_env.extensions + [e.value for e in test_env.secret_requires]
    requires = [Entry(x, where, kind="require") for x in d.requires if x not in loaded]
    set_by_test = {e.name for e in test_env.init_sql if e.kind == "set"}
    init_sql = [set_entry(name, value, where) for name, value in d.settings.items() if name not in set_by_test]
    init_sql += [Entry(sql.strip().rstrip(";") + ";", where, kind="sql", shown="") for sql in d.init]
    env, env_unset, env_state = {}, {}, {}
    for name, value in d.env.items():
        if name in test_env.env_state:
            continue
        if value is None:
            env_unset[name] = Entry(name, where, kind="unset")
            env_state[name] = "unset"
        else:
            env[name] = Entry(value, where, kind="env", name=name, secret=bool(SECRET_NAME.search(name)))
            env_state[name] = "set"
    return replace(
        test_env,
        requires=requires + test_env.requires,
        init_sql=init_sql + test_env.init_sql,
        env={**env, **test_env.env},
        env_unset={**env_unset, **test_env.env_unset},
        env_state={**env_state, **test_env.env_state},
    )


def batch_session(test_env) -> Session:
    """The Session of a `.test` body, from its test environment under its DuckDB engine
    (`environment()`): one Client, on the `unittest` engine of the batch that is given it."""
    return Session(test_env, [UnittestEngineDef("unittest", environment=test_env.key()).make()])


def planned(test, test_env, project) -> str:
    """The identity of the test's Session, before anything runs: for a `.test` body its batch's, for
    a Python body that of the engines it will lease over `test_env`. It is what a plan labels a batch
    by; a Python body's Session at run time also holds its own temp root."""
    if test.definition.type == SQLLOGIC:
        return batch_session(environment(test, test_env, project)).identity
    engines = sorted(planned_identity(d) for d in bind(test, test_env, project))
    return digest({"engines": engines, "environment": test_env.key()})


def lines(test, project) -> list[str]:
    """What `scan --env` says of each engine the test names: the dialect of its Client, and what the
    engine gives."""
    out = []
    for d in named(test, project):
        dialect = "unittest" if test.definition.type == SQLLOGIC else d.dialect
        where = f"engine {d.id}"
        out.append(f"client: {d.id} ({dialect})  <- SERVICES {d.id}")
        if d.requires:
            out.append(f"  require: {', '.join(d.requires)}  <- {where}")
        for name, value in d.settings.items():
            out.append(f"  set: {name}={MASK if SECRET_NAME.search(name) else value}  <- {where}")
        if d.init:
            out.append(f"  init sql  <- {where}")
        for name, value in d.env.items():
            if value is None:
                out.append(f"  env unset: {name}  <- {where}")
            else:
                out.append(f"  env: {name}={MASK if SECRET_NAME.search(name) else value}  <- {where}")
    return out


def bind(test, test_env, project) -> list:
    """The engines the test leases, each bound to what the test gets."""
    engines = named(test, project)
    if not any(d.kind == DEFAULT.kind for d in engines):
        engines.insert(0, DEFAULT)
    inputs = [{d: mode} for d, mode in test.inputs]
    out = []
    for d in engines:
        if d.kind == DEFAULT.kind:
            d = d.with_(
                requires=[*d.requires, *(x for x in test_env.extensions if x not in d.requires)],
                init=[*d.init, *(e.value for e in test_env.init_sql if e.value)],
                env={**d.env, **_process_env(test_env)},
            )
        if inputs and d.dialect is not None:
            d = d.with_(inputs=[*d.inputs, *inputs])
        out.append(d)
    return out


def _process_env(test_env) -> dict:
    """The variables the test sets (a value) or unsets (None), in the order that decides; secrets
    are not among them: an engine delivers those in its own dialect."""
    return {
        name: test_env.env[name].value if action == "set" else None
        for name, action in test_env.env_state.items()
        if name not in test_env.passthrough
    }


def template_names(test_env, temp_dir=None) -> Names:
    """The names a Client's templates see: those a `.test` body sees. The test's values, and its
    roots as `{TEMP_DIR}` (the test's own place, under a storage's temp root when it has one, else
    `temp_dir`), `{DATA_DIR}`, and `{LOCAL_TEMP_DIR}` / `{LOCAL_DATA_DIR}` where the root is local or
    has a local twin. Never a secret: a credential reaches an engine as its preamble."""
    names = Names(test_env.value_map)
    roots = test_env.root_paths
    for name, root, local in (
        ("TEMP_DIR", roots.get("temp_dir_root") or temp_dir, roots.get("local_temp_dir_root")),
        ("DATA_DIR", roots.get("data_dir"), roots.get("local_data_dir")),
    ):
        if root is None:
            continue
        names[name] = root
        if local or "://" not in root:
            names[f"LOCAL_{name}"] = local or root
    return names


class Session:
    """The one Session of a test: a Client on each engine it leases, the values their templates
    see, and what the test gathers as it runs -- the checks that failed, the teardowns, the log of
    its statements. A value bound here (`bind`) reaches every Client; one bound on a Client
    reaches that engine only."""

    def __init__(self, test_env, engines=(), *, temp_dir=None):
        self.environment = test_env
        self.values = template_names(test_env, temp_dir)
        self.failed_checks, self.teardowns, self.statements = [], [], []
        self.clients = {}  # engine id -> the Client on the instance this test leases
        self._own = []  # clients on no lease: a shell a body starts for itself
        for engine in engines:
            self.clients[engine.id] = engine.client(**self._wiring())

    def _wiring(self):
        return {
            "values": self.values,
            "record": self.failed_checks.append,
            "defer": self.teardowns.append,
            "log": self.statements.append,
        }

    def bind(self, **values):
        """Values every Client's templates see from now on."""
        self.values.update(values)
        return self

    def client(self, name, *, table=None, **values):
        """The Client on the engine `name`; `table` is its `{t}`, `values` are for it alone."""
        if name not in self.clients:
            raise KeyError(f"engine {name!r} is not one this test leases (clients={sorted(self.clients)})")
        return self.clients[name].bind(table=table, **values)

    def own_client(self, name, run, *, table=None, engine=None):
        """A Client beside the leases: on a shell the body started for itself, or on a leased
        `engine` with a run of its own; logged with the rest."""
        client = Client(name, run, table=table, engine=engine, **self._wiring())
        self._own.append(client)
        return client

    @property
    def duckdb(self):
        """The Client on the test's DuckDB engine."""
        return next(c for c in self.clients.values() if c.engine.definition.kind == DEFAULT.kind)

    @property
    def identity(self) -> str:
        """The identities of the engine instances it leases, and the test environment."""
        engines = sorted(c.engine.identity for c in self.clients.values())
        return digest({"engines": engines, "environment": self.environment.key()})

    def seconds_by_client(self):
        totals = {}
        for client in [*self.clients.values(), *self._own]:
            totals[client.name] = totals.get(client.name, 0.0) + client.seconds
        return {name: round(seconds, 2) for name, seconds in totals.items()}
