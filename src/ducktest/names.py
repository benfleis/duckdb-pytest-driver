"""The names of one scan: every declaration the `ducktest_conf.py` files make, and every place, in them or in
a test body, that refers to one. A test file, or a helper it imports, declares nothing: that is an error.

A name is the label the user gives a resource (`S3ServiceDef.rustfs("local_s3")`,
`LOCAL_S3.storage("local_lake")`), and what a reference in the same project uses; identity stays the
content digest. While the loader runs the conf files, and the scan imports the bodies, it holds one
`Names` (a context variable): each constructor registers what it made, and each reference slot
(`storage=`, `credential=`, `service=`, `engines=[...]`, `scopes=[...]`, `container=`, `bucket=`, a matrix
value) takes a plain name or the object, records it, and gives a plain string on.

`derive()` makes the project's declared set from them, once the bodies are imported. What a rule, a matrix
value or a body's `@test(...)` names in a slot, or in a `{name.attr}` of its values, settings or init_sql,
is reached; a reached storage pulls its inputs and every credential scoped to it, a reached credential
the services among its inputs. The declared set is that, with what `Project(services=, storages=,
credentials=)` lists, and what each listed one pulls. Engines are not reached: `Project(engines=)` lists
them. The checks run on that set:

    collision                       two declarations of one name                    error
    undefined                       a reference to a name nothing declares          error, with the closest one
    `{name.attr}`                   an attribute the kind of `name` does not have   error
    engine not listed               a reference to an engine `engines=` lacks       error
    unused                          a declaration nothing reaches; one listed       warning; an error with --strict
                                    that nothing reaches; a matrix parameter no
                                    test runs over

Engines, services, storages and credentials share one namespace: a `{name.attr}` value names any of them.
A bucket or a container is named within its server.
"""

from __future__ import annotations

import contextvars
import difflib
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field

from .binary import UsageError

_PACKAGE = os.path.dirname(os.path.abspath(__file__))
_current: contextvars.ContextVar[Names | None] = contextvars.ContextVar("ducktest_names", default=None)
ATTRIBUTE_REF = re.compile(r"\{([\w-]+)\.(\w+)\}")  # {local_s3.host_port}: an attribute of a resource

# kind -> where a declaration of it is listed
BIND_IN = {
    "engine": "Project(engines=[...])",
    "service": "Project(services=[...])",
    "storage": "Project(storages=[...])",
    "credential": "Project(credentials=[...])",
    "bucket": "S3ServiceDef(buckets=[...])",
    "container": "AzureServiceDef(containers=[...])",
}
PARTS = ("bucket", "container")


def kind_of(obj) -> str | None:
    """The kind a reference slot speaks of, for a declaration; None for anything else."""
    from .resources import CredentialDef, EngineDef, ResourceDef, ServiceDef, StorageDef
    from .services.parts import Part

    for cls, kind in ((EngineDef, "engine"), (StorageDef, "storage"), (CredentialDef, "credential")):
        if isinstance(obj, cls):
            return kind
    if isinstance(obj, ServiceDef):
        return "service"
    if isinstance(obj, Part):
        return obj.noun
    if isinstance(obj, ResourceDef):
        return "resource"
    return None


@dataclass
class Declared:
    obj: object
    kind: str
    where: tuple | None  # (path, line) in the conf that made it

    @property
    def name(self) -> str:
        return self.obj.name


@dataclass
class Reference:
    kind: str
    name: str
    obj: object | None  # the declaration, when the slot was given the object
    where: tuple | None
    reach: bool = True  # a slot of a rule, a matrix value or @test: what it names is in the declared set


@dataclass
class Names:
    root: str = "."  # what a location is shown relative to
    conf: str | None = None  # the conf file that runs now
    body: str | None = None  # the test file the scan imports now
    confs: set = field(default_factory=set)  # every conf file the loader ran
    declared: list = field(default_factory=list)
    references: list = field(default_factory=list)
    _superseded: set = field(default_factory=set)  # id() of a declaration a derived one replaced

    # -- what the conf files do ---------------------------------------------------------
    def declare(self, obj) -> None:
        where = here()
        if self.body is not None:
            self._refuse_in_body(obj, where)
        self.declared.append(Declared(obj, kind_of(obj), where))

    def _refuse_in_body(self, obj, where):
        """A declaration belongs to a conf file: a test file, or a helper it imports, makes none."""
        from .declare import CONF_FILE

        if where is not None and where[0] in self.confs:
            raise UsageError(
                f"{self.show(_line_in(self.body) or where)}: imports {CONF_FILE}; "
                "a body gets the project's objects by name, not by import"
            )
        name = getattr(obj, "name", None)
        raise UsageError(
            f"{self.show(where)}: {type(obj).__name__}({name!r}) is declared in a test file; "
            f"declare it in {CONF_FILE} and bind it with @test(...)"
        )

    def supersede(self, old, new) -> None:
        """`new` was derived from `old` (`as_existing`, `with_`): it is the declaration from now on."""
        self._superseded.add(id(old))

    def refer(self, kind, name, obj=None, reach=True) -> None:
        self.references.append(Reference(kind, name, obj, here(), reach))

    def live(self) -> list[Declared]:
        return [d for d in self.declared if id(d.obj) not in self._superseded]

    def show(self, where) -> str:
        if where is None:
            return "?"
        path, line = where
        return f"{os.path.relpath(path, self.root)}:{line}"

    def declared_at(self, name, kind=None) -> list[Declared]:
        return [d for d in self.live() if d.name == name and (kind is None or d.kind == kind)]

    # -- the declared set and the checks, once every conf and body has run ----------------
    def derive(self, project, *, run_over=None, strict=False, everything=False) -> tuple[dict, list[str]]:
        """The project's declared set, kind -> {name: Def}: what the rules, the matrix values and the
        bodies reach, each with what it pulls, and what `Project(...)` lists. Raise on the first error;
        return the set and the warnings (errors with `strict`). `run_over`: the parameters some test runs
        over; `everything`: every declaration counts as reached (cleanup, which reads no body)."""
        self._collisions()
        registry = {d.name: d.obj for d in self.live() if d.kind not in PARTS}
        listed = [d for group in project.listed.values() for d in group]
        for d in listed + [r.obj for r in self.references if r.obj is not None and r.kind not in PARTS]:
            registry.setdefault(d.name, d)
        for ref in self.references:
            if ref.kind not in PARTS:
                self._resolve(ref, registry, project.engines)
        reach = [registry[r.name] for r in self.references if r.reach and r.kind not in PARTS]
        reach += [registry[n] for n in _template_names(project) if n in registry]
        if everything:
            reach += [d for d in registry.values() if kind_of(d) != "engine"]
        reached = _closure(reach, registry)
        declared = _closure(listed + reach, registry)
        out = {"engine": dict(project.engines), "service": {}, "storage": {}, "credential": {}}
        for d in declared.values():
            kind = kind_of(d)
            if kind in ("service", "storage", "credential"):
                if out[kind].setdefault(d.name, d) is not d:
                    raise UsageError(f"two {kind}s are named {d.name!r}; a {kind} is one declaration")
        self._attributes(project, out, registry)
        if everything:
            return out, []
        warnings = self._unused(project, declared, reached | _default_engine(project.engines))
        warnings += [
            f"{project.project_file}: matrix parameter {p!r}: no rule or test runs over it; "
            f"give matrix=[{p!r}] where tests need it, or remove it"
            for p in project.matrix
            if run_over is not None and p not in run_over
        ]
        if warnings and strict:
            raise UsageError("--strict: declarations that nothing uses:\n  " + "\n  ".join(warnings))
        return out, warnings

    def _unused(self, project, declared, reached):
        listed = {id(d) for group in project.listed.values() for d in group}
        parts_of_declared = {id(p) for d in self.live() if d.kind not in PARTS for p in _parts(d.obj)}
        named_objects = {id(r.obj) for r in self.references if r.obj is not None}
        out = []
        for d in self.live():
            at = self.show(d.where)
            if d.kind in PARTS:
                if id(d.obj) not in parts_of_declared and id(d.obj) not in named_objects:
                    out.append(
                        f"{at}: {d.kind} {d.name!r} is declared and not used; bind it in {BIND_IN[d.kind]}, or remove it"
                    )
            elif d.kind == "engine":
                if d.name not in project.engines:
                    out.append(
                        f"{at}: engine {d.name!r} is declared and not listed; list it in {BIND_IN['engine']}, or remove it"
                    )
                elif id(d.obj) not in reached:
                    out.append(
                        f"{at}: engine {d.name!r} is listed and no test drives it; name it in engines=, or take it out"
                    )
            elif id(d.obj) not in declared:
                out.append(f"{at}: {d.kind} {d.name!r} is declared and nothing reaches it; {_REACH}, or remove it")
            elif id(d.obj) in listed and id(d.obj) not in reached:
                out.append(
                    f"{at}: {d.kind} {d.name!r} is listed in {BIND_IN.get(d.kind)} and nothing reaches it; "
                    f"{_REACH}, or take it out of the list"
                )
        return out

    def _collisions(self):
        by_name = {}
        for d in self.live():
            if d.kind not in PARTS:
                by_name.setdefault(d.name, []).append(d)
        for name, found in by_name.items():
            if len(found) > 1:
                where = ", ".join(f"{d.kind} at {self.show(d.where)}" for d in found)
                raise UsageError(
                    f"the name {name!r} is declared {len(found)} times: {where}; a name is one declaration"
                )

    def _resolve(self, ref, registry, engines):
        at = self.show(ref.where)
        target = registry.get(ref.name)
        if target is None:
            known = sorted(n for n, d in registry.items() if kind_of(d) == ref.kind)
            raise UsageError(f"{at}: {ref.kind}={ref.name!r} is not declared{_did_you_mean(ref.name, known)}")
        if kind_of(target) != ref.kind:
            raise UsageError(f"{at}: {ref.kind}={ref.name!r} names a {kind_of(target)}, not a {ref.kind}")
        if ref.kind == "engine" and ref.name not in engines:
            raise UsageError(f"{at}: {self._engine_not_listed(ref.name)}")

    def _engine_not_listed(self, name):
        found = self.declared_at(name, "engine")
        made = f" (declared at {self.show(found[0].where)})" if found else ""
        return f"engine {name!r}{made} is not one of the project's engines; list it in {BIND_IN['engine']}"

    def _attributes(self, project, out, registry):
        declared = {n: d for group in out.values() for n, d in group.items()}
        for where, text in _templates(project, out["storage"].values()):
            for m in ATTRIBUTE_REF.finditer(text):
                name, attribute = m.groups()
                target = declared.get(name)
                if target is None:
                    if kind_of(registry.get(name)) == "engine":
                        raise UsageError(f"{where}: {m.group()}: {self._engine_not_listed(name)}")
                    raise UsageError(
                        f"{where}: {m.group()}: no resource is named {name!r}{_did_you_mean(name, sorted(declared))}"
                    )
                known = target.attribute_names()
                if known is not None and attribute not in known:
                    raise UsageError(
                        f"{where}: {m.group()}: a {kind_of(target)} has no attribute {attribute!r}; "
                        f"{name} has {', '.join(known) or 'none'}"
                    )


_REACH = "name it in a rule, a matrix value or @test(...)"


def _parts(obj):
    parts = getattr(obj, "parts", None)
    return parts() if callable(parts) else ()


def _closure(roots, registry) -> dict:
    """id -> Def: `roots` and what each pulls in: its inputs, the declarations its templates name and,
    for a storage, every credential scoped to it."""
    from .resources import CredentialDef, StorageDef

    scoped = [d for d in registry.values() if isinstance(d, CredentialDef)]
    out = {}

    def walk(d):
        if id(d) in out:
            return
        out[id(d)] = d
        for _, _, sub in d.edges():
            if sub is not None:
                walk(sub)
        if isinstance(d, StorageDef):
            for text in _storage_templates(d):
                for m in ATTRIBUTE_REF.finditer(text):
                    if m.group(1) in registry:
                        walk(registry[m.group(1)])
            for c in scoped:
                if d.name in c.scope_storages:
                    walk(c)

    for d in roots:
        walk(d)
    return out


def _default_engine(engines) -> dict:
    """The project's one DuckDB engine, which a test that names none drives."""
    from .engines.duckdb import DuckDBEngineDef

    duckdb = [d for d in engines.values() if isinstance(d, DuckDBEngineDef)]
    return {id(duckdb[0]): duckdb[0]} if len(duckdb) == 1 else {}


def _storage_templates(storage):
    return [v for v in (getattr(storage, a, None) for a in ("root", "endpoint", "account")) if isinstance(v, str)]


def _places(project):
    """(label, Options) of every place that gives options: the rules, the matrix values, the bodies' @test."""
    from .declare import CONF_FILE

    out = []
    for conf_dir, rules in project._rules_by_dir.items():
        conf = os.path.relpath(os.path.join(conf_dir, CONF_FILE), project.root)
        out += [(f"{conf}: {rule.label}", rule.options) for rule in rules]
    for parameter, values in project.matrix.items():
        out += [(f"matrix[{parameter!r}][{name!r}]", options) for name, options in values.items()]
    return out + list(project.body_options)


def _templates(project, storages):
    """(label, text) of every string that may hold a `{name.attr}`: the values, settings and init_sql of the
    rules, matrix values and bodies, and the templates of `storages`."""
    out = []
    for label, options in _places(project):
        given = options.given
        for field_name in ("values", "settings"):
            for key, value in given.get(field_name, {}).items():
                if isinstance(value, str):
                    out.append((f"{label}: {field_name}[{key!r}]", value))
        out += [(f"{label}: init_sql[{n}]", sql) for n, sql in enumerate(given.get("init_sql", ())) if isinstance(sql, str)]
    for storage in storages:
        for attr in ("root", "endpoint", "account"):
            value = getattr(storage, attr, None)
            if isinstance(value, str):
                out.append((f"storage {storage.name!r}: {attr}", value))
    return out


def _template_names(project):
    """The names `{name.attr}` speaks of in the places' values, settings and init_sql."""
    return [m.group(1) for _, text in _templates(project, ()) for m in ATTRIBUTE_REF.finditer(text)]


def _did_you_mean(name, known) -> str:
    close = difflib.get_close_matches(name, known, n=1)
    if close:
        return f"; did you mean {close[0]!r}?"
    return f"; the declared ones are {', '.join(repr(k) for k in known)}" if known else ""


def here():
    """(path, line) of the innermost frame outside this package: the line of a conf that made a call."""
    frame = sys._getframe(1)
    while frame is not None:
        path = frame.f_code.co_filename
        if not path.startswith("<") and not os.path.abspath(path).startswith(_PACKAGE + os.sep):
            return (os.path.abspath(path), frame.f_lineno)
        frame = frame.f_back
    return None


def _line_in(path):
    """(path, line) of the innermost frame running `path`, if any."""
    frame = sys._getframe(1)
    while frame is not None:
        if os.path.abspath(frame.f_code.co_filename) == path:
            return (path, frame.f_lineno)
        frame = frame.f_back
    return None


def current() -> Names | None:
    return _current.get()


@contextmanager
def loading(names: Names):
    token = _current.set(names)
    try:
        yield names
    finally:
        _current.reset(token)


def declare(obj) -> None:
    """A constructor's registration: the declaration exists, bound or not."""
    names = _current.get()
    if names is not None:
        names.declare(obj)


def supersede(old, new):
    names = _current.get()
    if names is not None:
        names.supersede(old, new)
    return new


def ref(value, kind, where, *, reach=True):
    """What a reference slot gives on: the name of `value`, a plain string. `value` is a name or the
    object; either is recorded for the checks at the end of the scan. `reach=False`: the reference does
    not pull what it names into the declared set (a credential's `scopes=`)."""
    names = _current.get()
    if isinstance(value, str):
        if names is not None:
            names.refer(kind, value, reach=reach)
        return value
    if kind_of(value) == kind:
        if names is not None:
            names.refer(kind, value.name, value, reach)
        return value.name
    name = getattr(value, "name", None)
    shown = f"{type(value).__name__}({name!r})" if isinstance(name, str) else repr(value)
    raise UsageError(f"{where}: {kind} {shown}; give the name of a {kind}, or the {kind} itself")
