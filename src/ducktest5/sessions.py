"""The Sessions of one test: the ones it names, a default DuckDB one, one for each engine it uses.

Each is bound to what the test gets: its storage and credential inputs, and, for a DuckDB Session,
the test environment's requires, init SQL and process variables. What it is bound to is part of
its identity, so a test with other inputs gets another instance.

A `.test` body runs in one DuckDB Session, rendered in the `unittest` dialect: the process of its
batch. `environment()` puts that Session over the test environment, and the batch key is made of the
result, so two `.test` bodies share a batch exactly when their Sessions have one identity. What the
body itself says, an inline `require` among it, is not part of that.
"""

from __future__ import annotations

from dataclasses import replace

from .binary import UsageError
from .model import SQLLOGIC
from .provision import MASK, SECRET_NAME
from .resources import SessionDef
from .testenv import Entry, set_entry

DEFAULT = SessionDef("duckdb")


def named(test, project) -> list[SessionDef]:
    """The project's Sessions the test names, on `@test` or by a rule."""
    return [project.sessions[name] for name in test.definition.sessions] if project else []


def duckdb_of(test, project) -> SessionDef:
    """The DuckDB Session of a test: the one it names, else the default."""
    duckdb = [d for d in named(test, project) if d.engine == "duckdb"]
    return duckdb[0] if duckdb else DEFAULT


def check(definition, sessions):
    """A `.test` body runs in one DuckDB Session, and that Session takes the test's inputs only."""
    if definition.type != SQLLOGIC:
        return
    where = f"{definition.path}::{definition.name}"
    other = [d.id for d in sessions if d.engine != "duckdb"]
    if other:
        raise UsageError(f"{where} names sessions {other}; a .test body runs in a DuckDB Session only")
    if len(sessions) > 1:
        raise UsageError(f"{where} names sessions {[d.id for d in sessions]}; a .test body runs in one")
    if sessions and sessions[0].inputs:
        raise UsageError(
            f"{where} names session {sessions[0].id!r}, which has inputs of its own; bind them on the test instead"
        )


def environment(test, test_env, project):
    """`test_env` under the test's DuckDB Session, as the process of a `.test` batch is given it: the
    Session's requires, settings, init statements and process variables first. What `test_env`
    already gives is not given again, and where both set one name, the test's own is the more
    specific and wins."""
    d = duckdb_of(test, project)
    where = f"session {d.id}"
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


def lines(test, project) -> list[str]:
    """What `scan --env` says of each Session the test names: its dialect, and what it gives."""
    out = []
    for d in named(test, project):
        dialect = "unittest" if test.definition.type == SQLLOGIC else dialect_of(d, project.engines)
        where = f"session {d.id}"
        out.append(f"session: {d.id} ({dialect})  <- SESSIONS {d.id}")
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


def dialect_of(definition, engines) -> str | None:
    if definition.engine == "duckdb":
        return "duckdb"
    return engines[definition.engine].dialect


def bind(test, test_env, project) -> list[SessionDef]:
    sessions = named(test, project)
    if not any(d.engine == "duckdb" for d in sessions):
        sessions.insert(0, DEFAULT)
    covered = {d.engine for d in sessions}
    sessions += [SessionDef(e, engine=e) for e in test.engines if e not in covered]
    inputs = [{d: mode} for d, mode in test.inputs]
    out = []
    for d in sessions:
        if d.engine == "duckdb":
            d = d.with_(
                requires=[*d.requires, *(x for x in test_env.extensions if x not in d.requires)],
                init=[*d.init, *(e.value for e in test_env.init_sql if e.value)],
                env={**d.env, **_process_env(test_env)},
            )
        if inputs and dialect_of(d, project.engines) is not None:
            d = d.with_(inputs=[*d.inputs, *inputs])
        out.append(d)
    return out


def _process_env(test_env) -> dict:
    """The variables the test sets (a value) or unsets (None), in the order that decides; secrets
    are not among them: a Session delivers those in its own dialect."""
    return {
        name: test_env.env[name].value if action == "set" else None
        for name, action in test_env.env_state.items()
        if name not in test_env.passthrough
    }
