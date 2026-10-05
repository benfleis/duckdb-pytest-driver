"""The engines a test leases, and its identity.

A test names engines on `@test(engines=...)`, by a rule or by a MATRIX value; each gets a Client, and one
that names no DuckDB engine gets a Client on the default one: the project's DuckDB engine when it
declares exactly one, the built-in bare one when it declares none. With several, a test that names
none is a scan error. Each engine is bound to what the test gets:
its storage and credential inputs, and, for a DuckDB engine, the test environment's requires, init
SQL and process variables. What it is bound to is part of the engine's identity, so a test with
other inputs leases another instance.

A test's identity is that of the engine instances it leases and its test environment; tests of one
identity may share those instances, and `.test` bodies of one identity share a batch. A `.test` body
runs in one DuckDB engine, rendered in the `unittest` dialect: the process of its batch.
`environment()` puts that engine over the test environment, and `batch_identity()` is the identity
of a test that leases the `unittest` engine of a batch given the result. So two `.test` bodies share
a batch exactly when their DuckDB engines and test environments are equal. What the body itself
says, an inline `require` among it, is not part of that.
"""

from __future__ import annotations

from dataclasses import replace

from .binary import UsageError
from .engines.duckdb import duckdb_engine
from .engines.unittest import UnittestEngineDef
from .model import SQLLOGIC
from .provision import MASK, SECRET_NAME
from .resources.base import digest
from .resources.client import Names
from .resources.engine import Delivered
from .resources.made import planned_identity
from .testenv import Entry, set_entry

DEFAULT = duckdb_engine("duckdb")


def named(test, project) -> list:
    """The project's engines the test names, on `@test`, by a rule or by a MATRIX value."""
    return [project.engines[name] for name in test.engines] if project else []


def default_of(test, project):
    """The DuckDB engine of a test that names none."""
    declared = [d for d in project.engines.values() if d.kind == DEFAULT.kind] if project else []
    if len(declared) > 1:
        raise UsageError(
            f"{test.id} names no DuckDB engine, and the project declares {sorted(d.id for d in declared)}; "
            "name one with engines=, for every test with default(engines=[...])"
        )
    return declared[0] if declared else DEFAULT


def duckdb_of(test, project):
    """The DuckDB engine of a test: the one it names, else the default."""
    duckdb = [d for d in named(test, project) if d.kind == DEFAULT.kind]
    return duckdb[0] if duckdb else default_of(test, project)


def engines_of(test, project) -> list:
    """The engines a test runs on: those it names, and its DuckDB engine where it names none."""
    engines = named(test, project)
    if not any(d.kind == DEFAULT.kind for d in engines):
        engines.insert(0, default_of(test, project))
    return engines


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
    init_sql += [Entry(sql.strip().rstrip(";") + ";", where, kind="sql", shown="") for sql in d.init_sql]
    env, unset, state = {}, {}, {}
    for name, value in d.process_env.items():
        if name in test_env.process_state:
            continue
        if value is None:
            unset[name] = Entry(name, where, kind="unset")
            state[name] = "unset"
        else:
            env[name] = Entry(value, where, kind="env", name=name, secret=bool(SECRET_NAME.search(name)))
            state[name] = "set"
    return replace(
        test_env,
        requires=requires + test_env.requires,
        init_sql=init_sql + test_env.init_sql,
        process_env={**env, **test_env.process_env},
        process_unset={**unset, **test_env.process_unset},
        process_state={**state, **test_env.process_state},
    )


def identity(engine_identities, test_env) -> str:
    """A test's identity: the engine instances it leases, and its test environment."""
    return digest({"engines": sorted(engine_identities), "environment": test_env.key()})


def batch_identity(test_env) -> str:
    """The identity of a `.test` body, from its test environment under its DuckDB engine
    (`environment()`): it leases one engine, the `unittest` process of the batch that is given it."""
    return identity([UnittestEngineDef("unittest", environment=test_env.key()).make().identity], test_env)


def planned(test, test_env, project) -> str:
    """The test's identity before anything runs: for a `.test` body its batch's, for a Python body
    that of the engines it will lease over `test_env`. It is what a plan labels a batch by; a Python
    body's identity at run time also holds its own temp root."""
    if test.definition.type == SQLLOGIC:
        return batch_identity(environment(test, test_env, project))
    return identity([planned_identity(d) for d in bind(test, test_env, project)], test_env)


def lines(test, project) -> list[str]:
    """What `scan --env` says of each engine the test names: the dialect of its Client, and what the
    engine gives."""
    shown = [(d, f"ENGINES {d.id}") for d in named(test, project)]
    duckdb = duckdb_of(test, project)
    if duckdb is not DEFAULT and all(d is not duckdb for d, _ in shown):
        shown.insert(0, (duckdb, f"ENGINES {duckdb.id} (the only DuckDB engine)"))
    out = []
    for d, source in shown:
        dialect = "unittest" if test.definition.type == SQLLOGIC else d.dialect
        where = f"engine {d.id}"
        out.append(f"client: {d.id} ({dialect})  <- {source}")
        if d.requires:
            out.append(f"  require: {', '.join(d.requires)}  <- {where}")
        for name, value in d.settings.items():
            out.append(f"  set: {name}={MASK if SECRET_NAME.search(name) else value}  <- {where}")
        if d.init_sql:
            out.append(f"  init sql  <- {where}")
        for name, value in d.process_env.items():
            if value is None:
                out.append(f"  process env unset: {name}  <- {where}")
            else:
                out.append(f"  process env: {name}={MASK if SECRET_NAME.search(name) else value}  <- {where}")
    return out


def bind(test, test_env, project) -> list:
    """The engines the test leases, each bound to what the test gets."""
    engines = engines_of(test, project)
    inputs = [{d: mode} for d, mode in test.inputs]
    out = []
    for d in engines:
        if d.kind == DEFAULT.kind:
            d = d.with_(
                requires=[*d.requires, *(x for x in test_env.extensions if x not in d.requires)],
                init_sql=[*d.init_sql, *(e.value for e in test_env.init_sql if e.value)],
                process_env={**d.process_env, **_process_env(test_env)},
            )
        if inputs and d.dialect is not None:
            d = d.with_(inputs=[*d.inputs, *inputs])
        out.append(d)
    return out


def _process_env(test_env) -> dict:
    """The variables the test sets (a value) or unsets (None), in the order that decides; a
    credential's among them, read when the engine process starts."""
    out = {}
    for name, action in test_env.process_state.items():
        entry = test_env.process_env.get(name)
        if action != "set":
            out[name] = None
        elif callable(entry.value):
            out[name] = Delivered(entry.value, entry.source)
        else:
            out[name] = entry.value
    return out


def template_names(test_env, temp_dir=None) -> Names:
    """The names a Client's templates see: those a `.test` body sees. The test's values, and its
    roots as `{TEMP_DIR}` (the test's own place, under a storage's temp root when it has one, else
    `temp_dir`), `{TEMP_DIR_ROOT}` (the run level above it), `{DATA_DIR}`, and `{LOCAL_TEMP_DIR}` /
    `{LOCAL_DATA_DIR}` where the root is local or has a local twin. Never a secret: a credential
    reaches an engine as process env."""
    names = Names(test_env.value_map)
    roots = test_env.root_paths
    run_level = test_env.run_temp_root or roots.get("temp_dir_root") or temp_dir
    if run_level is not None:
        names["TEMP_DIR_ROOT"] = run_level
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
