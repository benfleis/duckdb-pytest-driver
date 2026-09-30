"""The test environment: everything the engine process of one test is given, in one record.

A test is a definition with one set of parameters. What a parameter contributes used to reach a
`.test` body (the batch's config and init file) and a Python body (the `ctx.duckdb()` preamble) by
two separate paths, which could and did disagree. `resolve()` builds this record once, after
provisioning and before exec, out of the rules, the definition's own declaration, the test's params and
the chosen value of every variant axis; both deliveries read it, under the test's DuckDB engine
(`sessions.py`), and nothing else, and the batch key of a test of a `.test` file is made of that.

    requires    extensions to load first: `require` in the init file, `LOAD` in the preamble
    init_sql    the statements after them, in order: the settings as `SET`, then the variants' SQL
    values      the names a body writes as `{NAME}`, and each axis name -> the id of its value
    roots       the binary's four roots, where a variant gives them; `ctx.roots` for a Python body
    env         the process environment of the engine, and `env_unset` what to take out of it

A storage input gives roots and values; a credential gives secrets, which reach the engine only as
process environment (named to the binary in `env_passthrough`), read when the process starts. For a
`.test` body, `secret_sql` creates the secret scoped to the storage in the init file, with `{NAME}`
where a value goes: the binary fills it from that environment.

Every entry carries the source that asked for it, so `scan --env` and a failed test can say where
each part came from. What an input gives is not known before its services are up, nor once they
failed to come up; `resolve()` lists those in `pending`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

from .binary import UsageError
from .provision import MASK
from .provision import SECRET_NAME as _SECRET_NAME
from .resources import RO, RW, ServiceDef, ServiceNotUp
from .resources.client import check_names
from .resources.dialect import pairs, render
from .resources.made import Made, services_in

ROOTS = ("data_dir", "local_data_dir", "temp_dir_root", "local_temp_dir_root")
_ROOT_OF = {
    "DATA_DIR": "data_dir",
    "LOCAL_DATA_DIR": "local_data_dir",
    "TEMP_DIR": "temp_dir_root",
    "LOCAL_TEMP_DIR": "local_temp_dir_root",
}

_LABELS = {
    "require": "require",
    "secret": "secret",
    "set": "set",
    "sql": "init sql",
    "value": "values",
    "root": "root",
    "env": "env",
    "unset": "env unset",
}


@dataclass(frozen=True)
class Entry:
    """One part of a test environment and what asked for it. `shown` is what printing says in place
    of the value: `""` for init SQL, which is named by its source only -- it may hold a secret."""

    value: object
    source: str
    kind: str = "value"
    name: str = ""
    shown: str | None = None
    secret: bool = False


def set_statement(name, value):
    return f"SET {name} = '{str(value).replace(chr(39), chr(39) * 2)}';"


@dataclass
class TestEnv:
    requires: list = field(default_factory=list)  # Entry(value=extension name)
    init_sql: list = field(default_factory=list)  # Entry(value=one statement), in delivery order
    secret_sql: list = field(default_factory=list)  # Entry(value=CREATE SECRET), for a .test body only
    secret_requires: list = field(default_factory=list)  # Entry(value=extension) that secret_sql needs
    values: dict = field(default_factory=dict)  # name -> Entry
    roots: dict = field(default_factory=dict)  # one of ROOTS -> Entry
    env: dict = field(default_factory=dict)  # name -> Entry
    env_unset: dict = field(default_factory=dict)  # name -> Entry(value=name)
    env_state: dict = field(default_factory=dict)  # name -> "set"/"unset", whichever last touched it
    pending: list = field(default_factory=list)  # Entry(value=field name) a service must fill in
    passthrough: list = field(default_factory=list)  # names of the credentials' secrets in `env`

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
        """The process environment for the engine of this test, or None when it is this process's."""
        if not self.changes_environment:
            return None
        base = os.environ if base is None else base
        out = dict(base)
        # Replay in axis order so an env_unset after an env from an earlier axis wins, and vice versa.
        for name, action in self.env_state.items():
            value = self.env[name].value if action == "set" else None
            value = value() if callable(value) else value
            if value is None:
                out.pop(name, None)
            else:
                out[name] = str(value)
        return out

    def plus(self, *, values=None, init_sql="", source="ctx.run_test()"):
        """A copy with what a Python body adds for the `.test` file beside it."""
        out = replace(self, values=dict(self.values), init_sql=list(self.init_sql))
        for name, value in check_names(values or {}, source).items():
            _put_value(out.values, name, _value_entry(name, value, source))
        if init_sql:
            out.init_sql.append(Entry(init_sql.strip().rstrip(";") + ";", source, kind="sql", shown=""))
        return out

    def key(self):
        """Two tests of `.test` files may share a unittest process when this is equal; everything the process
        is given is in it, and nothing else is. The scoped secrets are not: they follow from the
        inputs, which are, and are rendered for a `.test` body only, so a key does not depend on the
        kind of body."""
        return json.dumps(
            {
                "requires": self.extensions,
                "init_sql": [e.value for e in self.init_sql],
                "values": {k: str(e.value) for k, e in sorted(self.values.items())},
                "roots": {k: str(e.value) for k, e in sorted(self.roots.items())},
                # A secret read at delivery counts by where it is read from, never by its value.
                "env": {k: e.source if callable(e.value) else str(e.value) for k, e in sorted(self.env.items())},
                "env_unset": sorted(self.env_unset),
                "pending": sorted(e.source for e in self.pending),
            },
            sort_keys=True,
            default=str,
        )

    # -- what a reader sees ----------------------------------------------------
    def entries(self):
        """Every entry, in the order the engine is given them."""
        return [
            *self.requires,
            *self.secret_requires,
            *self.secret_sql,
            *self.init_sql,
            *self.values.values(),
            *self.roots.values(),
            *self.env.values(),
            *self.env_unset.values(),
            *self.pending,
        ]

    def _shown(self, entry):
        text = MASK if entry.secret else str(entry.value) if entry.shown is None else entry.shown
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


def _put_value(values, name, entry):
    """Names are case-insensitive: a later tier's `PATH` replaces an earlier tier's `path`."""
    for given in [n for n in values if n.lower() == name.lower()]:
        del values[given]
    values[name] = entry


def _value_entry(name, value, source):
    return Entry(value, source, name=name, secret=bool(_SECRET_NAME.search(name)))


def set_entry(name, value, source):
    return Entry(
        set_statement(name, value),
        source,
        kind="set",
        name=name,
        shown=str(value),
        secret=bool(_SECRET_NAME.search(name)),
    )


def resolve(test, made=None, key=None):
    """The test environment of `test`. `made` is what the run made, its services among them; None
    before provisioning, as `scan` sees it. An input that needs a service that is not up is listed
    in `pending`. With `key` (a Python body's), a storage gives the child it mints for `key`, so the
    temp roots are the test's own; without it they are the run's, and the binary adds the rest."""
    definition = test.definition
    env = TestEnv()

    # Two axis-tier entries (an id or an explicit value) naming one thing is a scan-time error, not
    # an overwrite; a rule or the test's params row may still lose to an axis value, silently.
    axis_given = {}

    def _axis_value(name, value, source):
        seen = axis_given.get(name.lower())
        if seen is not None and seen != source:
            raise UsageError(f"{test.id}: {name!r} is given by both {seen} and {source}")
        axis_given[name.lower()] = source
        _put_value(env.values, name, _value_entry(name, value, source))

    # An axis value's id is a name the body can use without the author repeating it.
    for axis, v in test.axis_values.items():
        _axis_value(axis, v.id, f"AXES {axis}")
    for name, value in definition.values.items():
        _put_value(env.values, name, _value_entry(name, value, definition.source_of("values", name)))
    if test.params_key is not None:
        for name, value in check_names(test.params, f"{test.id}: params {test.params_key!r}").items():
            _put_value(env.values, name, _value_entry(name, value, f"params {test.params_key!r}"))

    for name, value in definition.settings.items():
        env.init_sql.append(set_entry(name, value, definition.source_of("settings", name)))

    for axis, v in test.axis_values.items():
        where = f"AXES {axis}={v.id}"
        for extension in v.requires:
            if extension not in env.extensions:
                env.requires.append(Entry(extension, where, kind="require"))
        for name, value in v.settings.items():
            env.init_sql.append(set_entry(name, value, where))
        for name, value in check_names(v.values, where).items():
            _axis_value(name, value, where)
        for name, value in v.roots.items():
            if name not in ROOTS:
                raise UsageError(f"AXES {axis}={v.id}: root {name!r} is not one of {ROOTS}")
            env.roots[name] = Entry(value, where, kind="root", name=name)
        # One ordered pass in axis order: within an axis, its env entries set before its env_unset
        # entries remove; the last of the two to touch a name, across axes, wins (see environ()).
        for name, value in v.env.items():
            if value is None:
                env.env_unset[name] = Entry(name, where, kind="unset")
                env.env_state[name] = "unset"
                continue
            env.env[name] = Entry(value, where, kind="env", name=name, secret=bool(_SECRET_NAME.search(name)))
            env.env_state[name] = "set"
        for name in v.env_unset:
            env.env_unset[name] = Entry(name, where, kind="unset")
            env.env_state[name] = "unset"
        if v.init_sql:
            env.init_sql.append(Entry(v.init_sql.strip().rstrip(";") + ";", where, kind="sql", shown=""))
    scanning, made = made is None, made or Made()
    from_axes = dict(env.roots)
    instances = [_deliver(env, d, mode, scanning, _axis_value, made, key) for d, mode in test.inputs]
    if key is not None:
        # No storage to mint from: the test's own place under a variant's temp root is named the same way.
        for name in ("temp_dir_root", "local_temp_dir_root"):
            e = env.roots.get(name)
            if e is not None and e is from_axes.get(name):
                env.roots[name] = replace(e, value=f"{e.value}/{made.run_token}/{key}")
    if definition.body_file:
        for credential, storages in pairs(i for i in instances if i is not None):
            rendered = render("unittest", "secret", storages, credential)
            where = f"credential {credential.id}, storage {', '.join(s.id for s in storages)}"
            for extension in rendered.requires:
                if extension not in env.extensions + [e.value for e in env.secret_requires]:
                    env.secret_requires.append(Entry(extension, where, kind="require"))
            env.secret_sql += [Entry(sql, where, kind="secret") for sql in rendered.sql]
    return env


def _deliver(env, definition, mode, scanning, value, made, key=None):
    """One input's outputs into the test environment, and the instance that gave them, or None when
    they are not known yet. Nothing is allocated here and nothing is held: that is for the walk of
    the resource graph. At scan a credential that does not resolve, or one a callable source makes,
    is listed in `pending`: its value is not needed before the run."""
    service = isinstance(definition, ServiceDef)
    where = f"{'service' if service else definition.kind} {definition.id}"
    try:
        instance = made.make(definition, mode)
    except ServiceNotUp as exc:
        needs = ", ".join(s.id for s in services_in([definition]))
        kind = "root" if definition.kind == "storage" else "value" if service else "env"
        env.pending.append(
            Entry("inputs", where, kind=kind, shown=f"<from service {needs}>" if scanning else f"<{exc}>")
        )
        return None
    try:
        if scanning and any(callable(s) for s in getattr(definition, "sources", ())):
            raise UsageError("made by a callable source when the test runs")
        if not service:  # the run's one instance, already up
            instance.start()
        if key is not None and definition.kind == "storage":
            given = instance.acquire(RW, key)
            given.create()  # the binary makes a .test body's place; a Python body's is made here
        else:
            given = instance
            instance.release(instance.acquire(RO), RO)
        values, secrets = given.delivery()
    except UsageError as exc:
        if not scanning:
            raise
        env.pending.append(Entry("secrets", where, kind="env", shown=f"<{exc}>"))
        return None
    for name, out in values.items():
        if name in _ROOT_OF:
            # The binary derives a local root equal to its twin; it takes local_temp_dir_root only beside a remote one.
            if not (name.startswith("LOCAL_") and out.get() == values[name[len("LOCAL_") :]].get()):
                env.roots[_ROOT_OF[name]] = Entry(out.get(), where, kind="root", name=_ROOT_OF[name])
        else:
            value(name, out.get(), where)
    for name, out in secrets.items():
        if name in env.env:
            raise UsageError(f"{name!r} is given by both {env.env[name].source} and {where}")
        env.env[name] = Entry(out.value, f"{where}: {out.source}", kind="env", name=name, secret=True)
        env.env_state[name] = "unset" if out.unset else "set"
        env.passthrough.append(name)
    return instance
