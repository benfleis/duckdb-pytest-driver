"""The case environment: everything the engine process of one case is given, in one record.

A case is a test body with one set of parameters. What a parameter contributes used to reach a
`.test` body (the batch's config and init file) and a Python body (the `ctx.duckdb()` preamble) by
two separate paths, which could and did disagree. `resolve()` builds this record once, after
provisioning and before exec, out of the rules, the test's own declaration, the case's params and
the chosen value of every variant axis; both deliveries read it and nothing else, and the batch key
of a `.test` case is made of it.

    requires    extensions to load first: `require` in the init file, `LOAD` in the preamble
    init_sql    the statements after them, in order: the settings as `SET`, then the variants' SQL
    values      the names a body writes as `{NAME}`, and each axis name -> the id of its value
    roots       the binary's four roots, where a variant gives them; `ctx.roots` for a Python body
    env         the process environment of the engine, and `env_unset` what to take out of it
    services    service id -> the mode it was provisioned in and its block, secrets masked

Every entry carries the source that asked for it, so `scan --env` and a failed case can say where
each part came from. A part a variant computes from the services' blocks is not known before
provisioning, nor once provisioning has run if a service it needs did not come up; `resolve()`
lists those in `pending` instead of calling the variant's code with an incomplete `blocks`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

from .binary import UsageError
from .provision import MASK
from .provision import SECRET_NAME as _SECRET_NAME

ROOTS = ("data_dir", "local_data_dir", "temp_dir_root", "local_temp_dir_root")

_LABELS = {
    "require": "require",
    "set": "set",
    "sql": "init sql",
    "value": "values",
    "root": "root",
    "env": "env",
    "unset": "env unset",
    "service": "service",
}


@dataclass(frozen=True)
class Entry:
    """One part of a case environment and what asked for it. `shown` is what printing says in place
    of the value: `""` for init SQL, which is named by its source only -- it may hold a secret."""

    value: object
    source: str
    kind: str = "value"
    name: str = ""
    shown: str | None = None
    secret: bool = False


def set_statement(name, value):
    return f"SET {name} = '{str(value).replace(chr(39), chr(39) * 2)}';"


def _masked(text, secrets):
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


@dataclass
class CaseEnv:
    requires: list = field(default_factory=list)  # Entry(value=extension name)
    init_sql: list = field(default_factory=list)  # Entry(value=one statement), in delivery order
    values: dict = field(default_factory=dict)  # name -> Entry
    roots: dict = field(default_factory=dict)  # one of ROOTS -> Entry
    env: dict = field(default_factory=dict)  # name -> Entry
    env_unset: dict = field(default_factory=dict)  # name -> Entry(value=name)
    services: dict = field(default_factory=dict)  # service id -> Entry(value={"mode", "block"})
    pending: list = field(default_factory=list)  # Entry(value=field name) a service must fill in
    secrets: tuple = ()  # what must never be printed: the secret fields of the services' blocks

    # -- what the deliveries read ---------------------------------------------
    @property
    def extensions(self):
        return [e.value for e in self.requires]

    @property
    def sql(self):
        return "\n".join(e.value for e in self.init_sql)

    @property
    def value_map(self):
        return {name: e.value for name, e in self.values.items()}

    @property
    def names(self):
        """`{NAME}` for a `.test` body, upper-cased as a .test writes them."""
        return {name.upper(): e.value for name, e in self.values.items()}

    @property
    def root_paths(self):
        return {name: e.value for name, e in self.roots.items()}

    @property
    def changes_environment(self):
        return bool(self.env or self.env_unset)

    def environ(self, base=None):
        """The process environment for the engine of this case, or None when it is this process's."""
        if not self.changes_environment:
            return None
        base = os.environ if base is None else base
        out = {k: v for k, v in base.items() if k not in self.env_unset}
        out.update({k: str(e.value) for k, e in self.env.items()})
        return out

    def plus(self, *, values=None, init_sql="", source="ctx.run_test()"):
        """A copy with what a Python body adds for the `.test` file beside it."""
        out = replace(self, values=dict(self.values), init_sql=list(self.init_sql))
        for name, value in (values or {}).items():
            out.values[name] = Entry(value, source, name=name)
        if init_sql:
            out.init_sql.append(Entry(init_sql.strip().rstrip(";") + ";", source, kind="sql", shown=""))
        return out

    def key(self):
        """Two `.test` cases may share a unittest process when this is equal; everything the process
        is given is in it, and nothing else is."""
        return json.dumps(
            {
                "requires": self.extensions,
                "init_sql": [e.value for e in self.init_sql],
                "values": {k: str(e.value) for k, e in sorted(self.values.items())},
                "roots": {k: str(e.value) for k, e in sorted(self.roots.items())},
                "env": {k: str(e.value) for k, e in sorted(self.env.items())},
                "env_unset": sorted(self.env_unset),
                "services": sorted(self.services),
            },
            sort_keys=True,
            default=str,
        )

    # -- what a reader sees ----------------------------------------------------
    def entries(self):
        """Every entry, in the order the engine is given them."""
        return [
            *self.requires,
            *self.init_sql,
            *self.values.values(),
            *self.roots.values(),
            *self.env.values(),
            *self.env_unset.values(),
            *self.services.values(),
            *self.pending,
        ]

    def _shown(self, entry):
        text = MASK if entry.secret else _masked(str(entry.value) if entry.shown is None else entry.shown, self.secrets)
        return f"{entry.name}={text}" if entry.name else text

    def lines(self):
        """One line for each entry: `what it is: what it says  <- where it came from`."""
        out = []
        for e in self.entries():
            label, shown = _LABELS[e.kind], self._shown(e)
            out.append(f"{label}: {shown}  <- {e.source}" if shown else f"{label}  <- {e.source}")
        return out

    def record(self):
        """The same, for `01-tests.yaml` and the report."""
        return [{"what": _LABELS[e.kind], "is": self._shown(e), "from": e.source} for e in self.entries()]


def _service_entry(service_id, provisioned, source):
    p = provisioned.get(service_id) if provisioned is not None else None
    if p is None:
        shown = f"{service_id} (did not come up)" if provisioned is not None else service_id
        return Entry(service_id, source, kind="service", shown=shown)
    return Entry(
        {"id": service_id, "mode": p.mode, "block": p.redacted()},
        source,
        kind="service",
        shown=f"{service_id} ({p.mode})",
    )


def _set_entry(name, value, source):
    return Entry(
        set_statement(name, value),
        source,
        kind="set",
        name=name,
        shown=str(value),
        secret=bool(_SECRET_NAME.search(name)),
    )


def _from_services(variant):
    return "<from service " + ", ".join(variant.services) + ">" if variant.services else "<from a service>"


def resolve(case, provisioned=None):
    """The case environment of `case`. `provisioned` maps a service id to its `Provisioned`; with
    None (before provisioning, as `scan` sees it) a part a variant computes from the blocks is
    listed in `pending` instead of being called."""
    blocks = {sid: p.block for sid, p in provisioned.items()} if provisioned is not None else None
    test = case.test
    env = CaseEnv(
        secrets=tuple(
            str(p.block[k]) for p in (provisioned or {}).values() for k in p.service.secrets if p.block.get(k)
        )
    )

    # An axis value's id is a name the body can use without the author repeating it.
    for axis, v in case.axis_values.items():
        env.values[axis] = Entry(v.id, f"AXES {axis}", name=axis)
    for name, value in test.values.items():
        env.values[name] = Entry(value, test.source_of("values", name), name=name)
    if case.case_id is not None:
        for name, value in case.params.items():
            env.values[name] = Entry(value, f"case {case.case_id!r}", name=name)

    for name, value in test.settings.items():
        env.init_sql.append(_set_entry(name, value, test.source_of("settings", name)))
    for service_id in test.services:
        env.services[service_id] = _service_entry(service_id, provisioned, test.source_of("services", service_id))

    for axis, v in case.axis_values.items():
        where = f"AXES {axis}={v.id}"
        for extension in v.requires:
            if extension not in env.extensions:
                env.requires.append(Entry(extension, where, kind="require"))
        for name, value in v.settings.items():
            env.init_sql.append(_set_entry(name, value, where))
        for name, value in _given(v, "values", blocks, env, where).items():
            env.values[name] = Entry(value, where, name=name)
        for name, value in _given(v, "roots", blocks, env, where).items():
            if name not in ROOTS:
                raise UsageError(f"AXES {axis}={v.id}: root {name!r} is not one of {ROOTS}")
            env.roots[name] = Entry(value, where, kind="root", name=name)
        for name, value in v.env.items():
            env.env[name] = Entry(value, where, kind="env", name=name, secret=bool(_SECRET_NAME.search(name)))
        for name in v.env_unset:
            env.env_unset[name] = Entry(name, where, kind="unset")
        for service_id in v.services:
            env.services.setdefault(service_id, _service_entry(service_id, provisioned, where))
        if callable(v.init_sql):
            if _ready(v, blocks):
                sql = _call(v.init_sql, blocks, where, "init sql")
                if sql:
                    env.init_sql.append(Entry(sql.strip().rstrip(";") + ";", where, kind="sql", shown=""))
            else:
                env.init_sql.append(Entry("", where, kind="sql", shown=""))  # a function of blocks we do not have
        elif v.init_sql:
            env.init_sql.append(Entry(v.init_sql.strip().rstrip(";") + ";", where, kind="sql", shown=""))
    return env


def _ready(variant, blocks):
    """Whether every service the variant declares is up: only then can its callables be called
    without one missing from `blocks` raising a `KeyError` inside someone else's lambda."""
    return blocks is not None and all(service_id in blocks for service_id in variant.services)


def _call(fn, blocks, where, field_name):
    """A variant's callable `values`/`roots`/`init_sql`, called; its own exception becomes a usage
    error naming what asked for it, never a traceback out of `resolve()`."""
    try:
        return fn(blocks)
    except Exception as exc:
        raise UsageError(f"{where}: {field_name} could not be computed: {type(exc).__name__}: {exc}") from exc


def _given(variant, field_name, blocks, env, where):
    """A variant's `values` or `roots`: a dict, or a function of the provisioned services' blocks."""
    given = getattr(variant, field_name)
    if not callable(given):
        return dict(given or {})
    if not _ready(variant, blocks):
        kind = "value" if field_name == "values" else "root"
        env.pending.append(Entry(field_name, where, kind=kind, shown=_from_services(variant)))
        return {}
    return dict(_call(given, blocks, where, field_name) or {})
