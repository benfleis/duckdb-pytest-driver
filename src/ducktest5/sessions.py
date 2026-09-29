"""The Sessions of one test: the ones it names, a default DuckDB one, one for each engine it uses.

Each is bound to what the test gets: its storage and credential inputs, and, for a DuckDB Session,
the test environment's requires, init SQL and process variables. What it is bound to is part of
its identity, so a test with other inputs gets another instance.
"""

from __future__ import annotations

from .resources import SessionDef

DEFAULT = SessionDef("duckdb")


def dialect_of(definition, engines) -> str | None:
    if definition.engine == "duckdb":
        return "duckdb"
    return engines[definition.engine].dialect


def bind(test, test_env, project) -> list[SessionDef]:
    named = [project.sessions[name] for name in test.definition.sessions]
    if not any(d.engine == "duckdb" for d in named):
        named.insert(0, DEFAULT)
    covered = {d.engine for d in named}
    named += [SessionDef(e, engine=e) for e in test.engines if e not in covered]
    inputs = [{d: mode} for d, mode in test.inputs]
    out = []
    for d in named:
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
