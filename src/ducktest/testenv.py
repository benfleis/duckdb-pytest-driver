"""The test environment: everything the engine process of one test is given, in one record.

A test is one cell of a definition's matrix. A `.test` body (the batch's config and init
file) and a Python body (its engines' preamble) read this one record, so they cannot disagree.
`resolve()` builds it once, after
provisioning and before exec, out of the rules, the definition's own declaration and the values of
its cell; both deliveries read it, under the test's DuckDB engine
(`leases.py`), and nothing else, and the batch key of a test of a `.test` file is made of that.

    requires    extensions to load first: `require` in the init file, `LOAD` in the preamble
    init_sql    the statements after them, in order: the settings as `SET`, then the `init_sql`
    values      the names a body writes as `{NAME}`, and each project matrix parameter -> the name of its value
    roots       the binary's four roots, from the storages `data=` and `temp=` bind; a Python body
                reads them as `ctx.values.temp_dir`, `ctx.values.data_dir` and their `local_` twins
    process_env the process environment of the engine: a value sets a variable, None unsets it

A storage gives what the slots that bind it say: `storage=` its values, `data=` and `temp=` the
roots; the default storage gives the roots of a slot nothing binds. A credential gives its fields,
which reach every engine process only as SDK env (named to the binary in `env_passthrough`), read
when the process starts. `secret_sql` is the secret scoped to the storages of the test, whichever
slot bound them: their connection options and the
scope, never a key. A `.test` body gets it in the init file; a Python body's engine renders the same.

Every entry carries the source that asked for it, so `scan --env` and a failed test can say where
each part came from. What an input gives is not known before its services are up, nor once they
failed to come up; `resolve()` lists those in `pending`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from .binary import UsageError
from .provision import MASK
from .provision import SECRET_NAME as _SECRET_NAME
from .resources import RO, ServiceDef, ServiceNotUp
from .resources.client import check_names
from .resources.credential import CredentialView, credential_types, engine_env, kept_names, removed_names
from .resources.dialect import pairs, render, secret_name
from .resources.made import Made, services_in

_LABELS = {
    "require": "require",
    "secret": "secret",
    "set": "set",
    "sql": "init sql",
    "value": "values",
    "root": "root",
    "env": "process env",
    "unset": "process env unset",
    "unserved": "not served",
    "removed": "process env removed",
    "kept": "process env kept",
}
_ERRNO = re.compile(r"\[Errno \d+\] ")
_INPUT_REF = re.compile(r"\{([\w-]+)\.(\w+)\}")  # {s3.host_port}: an attribute of an input


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
    secret_sql: list = field(default_factory=list)  # Entry(value=CREATE SECRET); the init file of a .test body has it
    unrendered: list = field(default_factory=list)  # Entry(shown="none (why)"): a credential with no secret
    shell_env: list = field(default_factory=list)  # Entry(value=names) the shell's env holds and an engine does not
    credentials: list = field(default_factory=list)  # CredentialView of each credential it holds, in order
    secret_requires: list = field(default_factory=list)  # Entry(value=extension) that secret_sql needs
    require_why: dict = field(default_factory=dict)  # extension -> what needs it, said where it is missing
    values: dict = field(default_factory=dict)  # name -> Entry
    roots: dict = field(default_factory=dict)  # data_dir, temp_dir_root or a local_ twin -> Entry
    process_env: dict = field(default_factory=dict)  # name -> Entry
    process_unset: dict = field(default_factory=dict)  # name -> Entry(value=name)
    process_state: dict = field(default_factory=dict)  # name -> "set"/"unset", whichever last touched it
    pending: list = field(default_factory=list)  # Entry(value=field name) a service must fill in
    pending_env: list = field(default_factory=list)  # env names a credential pending at scan may deliver
    passthrough: list = field(default_factory=list)  # names of the credentials' secrets in `env`
    run_temp_root: str | None = None  # a Python body's TEMP_DIR_ROOT: the run level above its own place

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

    def environ(self, base=None):
        """The process environment for the engine of this test: `base`, by default the shell's without
        the names an SDK reads (`engine_env`), then what the test sets and unsets."""
        out = engine_env() if base is None else dict(base)
        # Replay in parameter order so an unset after a set from an earlier parameter wins, and vice versa.
        for name, action in self.process_state.items():
            value = self.process_env[name].value if action == "set" else None
            value = value() if callable(value) else value
            if value is None:
                out.pop(name, None)
            else:
                out[name] = str(value)
        return out

    def plus(self, *, values=None, init_sql="", source="ctx.run_paired()"):
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
                "process_env": {
                    k: e.source if callable(e.value) else str(e.value) for k, e in sorted(self.process_env.items())
                },
                "process_unset": sorted(self.process_unset),
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
            *self.unrendered,
            *self.init_sql,
            *self.values.values(),
            *self.roots.values(),
            *self.process_env.values(),
            *self.process_unset.values(),
            *self.shell_env,
            *self.pending,
        ]

    def _shown(self, entry):
        text = MASK if entry.secret else _plain(entry.value) if entry.shown is None else entry.shown
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


def _plain(value):
    """A value as printed: one read at delivery (a credential's region) is read here too."""
    if callable(value):
        try:
            value = value()
        except Exception as exc:
            return f"<not known: {exc}>"
    return str(value)


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


def resolve(test, made=None, key=None, default=None):
    """The test environment of `test`. `made` is what the run made, its services among them; None
    before provisioning, as `scan` sees it. An input that needs a service that is not up is listed
    in `pending`. With `key` (a Python body's), the storage `temp=` binds gives the place under its
    run level for `key`, so the temp roots are the test's own; without it they are the run's, and the
    binary adds the rest. `default`, the run's DefaultStorage, gives the roots of a slot nothing binds."""
    definition, options = test.definition, test.options
    env = TestEnv()

    for name, value in options.values.items():
        _put_value(env.values, name, _value_entry(name, value, options.source_of("values", name)))
    given = {name.lower(): e.source for name, e in env.values.items()}

    def _input_value(name, value, source):
        seen = given.get(name.lower())
        if seen is not None and seen != source:
            raise UsageError(f"{test.name}: {name!r} is given by both {seen} and {source}")
        given[name.lower()] = source
        _put_value(env.values, name, _value_entry(name, value, source))

    for extension in options.requires:
        env.requires.append(Entry(extension, options.source_of("requires", extension), kind="require"))
    for name, value in options.settings.items():
        env.init_sql.append(set_entry(name, value, options.source_of("settings", name)))
    for n, sql in enumerate(options.init_sql):
        env.init_sql.append(
            Entry(sql.strip().rstrip(";") + ";", options.source_of("init_sql", n), kind="sql", shown="")
        )
    for name, value in options.process_env.items():
        source = options.source_of("process_env", name)
        if value is None:
            env.process_unset[name] = Entry(name, source, kind="unset")
            env.process_state[name] = "unset"
        else:
            env.process_env[name] = Entry(value, source, kind="env", name=name, secret=bool(_SECRET_NAME.search(name)))
            env.process_state[name] = "set"
    scanning, made = made is None, made or Made()
    roles = getattr(test, "roles", {})
    instances = [
        _deliver(env, d, mode, scanning, _input_value, made, key, {r: at for r, (s, at) in roles.items() if s is d})
        for d, mode in test.inputs
    ]
    _fill_input_refs(env, instances)
    if default is not None:
        _default_roots(env, default, made.run_token, key, roles)
    if key is not None:
        # This is the test's own place, not the run's: printed as TEMP_DIR so a reader does not
        # mistake it for the run level, which `scan --env` shows (there, key is always None).
        for internal, display in (("temp_dir_root", "TEMP_DIR"), ("local_temp_dir_root", "LOCAL_TEMP_DIR")):
            if internal in env.roots:
                env.roots[internal] = replace(env.roots[internal], name=display)
    held = [i for i in instances if i is not None]
    scoped = {c.name: storages for c, storages in pairs(held)}
    for credential in (i for i in held if i.definition.kind == "credential"):
        storages = scoped.get(credential.name)
        rendered = bool(storages)
        env.credentials.append(_view(credential, storages or [], rendered))
        if not rendered:
            shown = "none (no storage of the test is in its scope)"
            env.unrendered.append(Entry("", f"credential {credential.name}", kind="secret", shown=shown))
            continue
        # The same text either way; the init file of a .test body also requires what it needs.
        rendered = render("unittest" if definition.body_file else "duckdb", "secret", storages, credential)
        where = f"credential {credential.name}, storage {', '.join(s.name for s in storages)}"
        for extension in rendered.requires:
            if extension not in env.extensions + [e.value for e in env.secret_requires]:
                env.secret_requires.append(Entry(extension, where, kind="require"))
        env.require_why = {**rendered.why, **env.require_why}
        env.secret_sql += [Entry(sql, where, kind="secret") for sql in rendered.sql]
    _shell_env(env)
    return env


def _fill_input_refs(env, instances):
    """Each `{name.attribute}` in a value, where `name` is an input the test holds or an input of one, becomes
    that input's attribute. One whose input is not made yet (at scan, or its service is down) stays
    as written: the test is pending on that input already."""
    by_id = {}

    def walk(instance):
        if instance is not None and instance.name not in by_id:
            by_id[instance.name] = instance
            for sub in instance.inputs.values():
                walk(sub)

    for instance in instances:
        walk(instance)

    def one(m, name):
        ref, attribute = m.groups()
        if ref not in by_id:
            return m.group()
        if attribute not in by_id[ref].attributes:
            raise UsageError(f"value {name}: {{{ref}.{attribute}}}: {ref} has no attribute {attribute!r}")
        return str(by_id[ref].attributes[attribute].get())

    for name, entry in list(env.values.items()):
        if isinstance(entry.value, str) and _INPUT_REF.search(entry.value):
            env.values[name] = replace(entry, value=_INPUT_REF.sub(lambda m: one(m, name), entry.value))


def _view(credential, storages, rendered):
    d = credential.definition
    cls = type(d)
    fields = {name: MASK if cls.fields[name].secret else a.get() for name, a in credential.fields.items()}
    return CredentialView(
        name=d.name,
        type=cls.type,
        secret_name=secret_name(d) if rendered else None,
        storages=tuple(s.name for s in storages),
        scope=[s.attribute_values()["root"] for s in storages],
        env=tuple(credential.secrets),
        fields=fields,
        source=credential.source,
    )


def _shell_env(env):
    """What the shell's env holds that no engine process of the test gets, and what `inherit_env`
    keeps: by name only, never a value."""
    own = set(env.process_state)
    for cls in credential_types():
        names = [n for n in removed_names() if n.startswith(cls.sdk_prefixes) and n not in own]
        if names:
            prefixes = ", ".join(cls.sdk_prefixes)
            source = f"the shell's env ({prefixes} of {cls.__name__})"
            env.shell_env.append(Entry(", ".join(names), source, kind="removed"))
    if kept := kept_names():
        env.shell_env.append(Entry(", ".join(kept), "inherit_env", kind="kept"))


def _default_roots(env, default, run_token, key, roles):
    """What the default storage gives: the roots of the slots nothing binds; a slot bound to a storage
    whose service is not up yet is pending, not the default's."""
    temp = default.run_root(run_token) if key is None else default.test_dir(run_token, key)
    for name, local, value, bound in (
        ("temp_dir_root", "local_temp_dir_root", temp, "temp" in roles),
        ("data_dir", "local_data_dir", default.data_dir, "data" in roles),
    ):
        if not bound and name not in env.roots and local not in env.roots:
            env.roots[name] = Entry(value, "default storage", kind="root", name=name)
            if name == "temp_dir_root" and key is not None:
                env.run_temp_root = default.run_root(run_token)


def _pending_env(env, definition):
    if definition.kind == "credential":
        env.pending_env += [n for names in type(definition).env.values() for n in names]


def _deliver(env, definition, mode, scanning, value, made, key=None, roles=None):
    """One input's attributes into the test environment, and the instance that gave them, or None when
    they are not known yet. Nothing is allocated here and nothing is held: that is for the walk of
    the resource graph. At scan a credential that does not resolve, or one a callable source makes,
    is listed in `pending`: its value is not needed before the run. `roles` are the slots that bind a
    storage, each with its `.at()` path or None: `storage` gives its values, `data` and `temp` its roots."""
    roles = roles or {}
    service = isinstance(definition, ServiceDef)
    where = f"{'service' if service else definition.kind} {definition.name}"
    try:
        instance = made.make(definition, mode)
    except ServiceNotUp as exc:
        needs = [s.name for s in services_in([definition])]
        if scanning:
            kind = "root" if {"data", "temp"} & set(roles) else "value" if service or roles else "env"
            env.pending.append(Entry("inputs", where, kind=kind, shown=f"<from service {', '.join(needs)}>"))
            _pending_env(env, definition)
        else:
            down = [s for s in needs if s in made.errors] or needs
            why = "; ".join(f"{s} did not come up ({_ERRNO.sub('', made.errors.get(s, str(exc)))})" for s in down)
            env.pending.append(Entry("inputs", where, kind="unserved", shown=why))
        return None
    try:
        if scanning and any(callable(s) for s in getattr(definition, "sources", ())):
            raise UsageError("made by a callable source when the test runs")
        if not service:  # the run's one instance, already up
            instance.start()
        instance.release(instance.acquire(RO), RO)
        values, secrets = instance.delivery()
    except UsageError as exc:
        if not scanning and definition.kind != "credential":
            raise
        # A credential that does not resolve at run time blocks its test; this is what it says.
        env.pending.append(Entry("secrets", where, kind="env" if scanning else "unserved", shown=f"<{exc}>"))
        if scanning:
            _pending_env(env, definition)
        return None
    if definition.kind == "storage":
        if "storage" not in roles:
            values = {}
        if "temp" in roles:
            own = instance.temp_dir(roles["temp"], key)
            if key is not None:
                instance.make_place(own)  # the binary makes a .test body's place; a Python body's is made here
                env.run_temp_root = instance.temp_dir(roles["temp"])
            env.roots["temp_dir_root"] = Entry(own, where, kind="root", name="temp_dir_root")
        if "data" in roles:
            env.roots["data_dir"] = Entry(instance.data_dir(roles["data"]), where, kind="root", name="data_dir")
    for name, out in values.items():
        value(name, out.get(), where)
    secret_names = type(definition).secret_env() if definition.kind == "credential" else ()
    for name, out in secrets.items():
        if name in env.process_env:
            raise UsageError(f"{name!r} is given by both {env.process_env[name].source} and {where}")
        source = f"{where} ({type(definition).__name__}, from {out.source})"
        env.process_env[name] = Entry(out.value, source, kind="env", name=name, secret=name in secret_names)
        env.process_state[name] = "unset" if out.unset else "set"
        env.passthrough.append(name)
    return instance
