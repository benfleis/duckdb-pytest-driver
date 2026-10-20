"""The names of one load: every declaration a `ducktest_conf.py` makes, and every place that refers to one.

A name is the label the user gives a resource (`S3ServiceDef.rustfs("local_s3")`,
`LOCAL_S3.storage("local_lake")`), and what a reference in the same project uses; identity stays the
content digest. While the loader runs the conf files
it holds one `Names` (a context variable): each constructor registers what it made, and each reference slot
(`storage=`, `credential=`, `service=`, `engines=[...]`, `scopes=[...]`, `container=`, `bucket=`, a matrix
value) takes a plain name or the object, records it, and gives a plain string on. So a resource that exists
but was never bound is known, and `check()` can say where to bind it.

    collision                       two declarations of one name                    error
    undefined                       a reference to a name nothing declares          error
    `{name.attr}`                   an attribute the kind of `name` does not have   error
    not bound                       a reference to a declaration the project lacks  error, naming where to bind it
    unused                          a declaration nothing binds or names            warning; an error with --strict

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

# kind -> where a declaration of it is bound
BIND_IN = {
    "engine": "Project(engines=[...])",
    "service": "Project(services=[...]), or as an input of a bound storage, credential or engine",
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


@dataclass
class Names:
    root: str = "."  # what a location is shown relative to
    conf: str | None = None  # the conf file that runs now
    declared: list = field(default_factory=list)
    references: list = field(default_factory=list)
    _superseded: set = field(default_factory=set)  # id() of a declaration a derived one replaced

    # -- what the conf files do ---------------------------------------------------------
    def declare(self, obj) -> None:
        self.declared.append(Declared(obj, kind_of(obj), here()))

    def supersede(self, old, new) -> None:
        """`new` was derived from `old` (`as_existing`, `with_`): it is the declaration from now on."""
        self._superseded.add(id(old))

    def refer(self, kind, name, obj=None) -> None:
        self.references.append(Reference(kind, name, obj, here()))

    def live(self) -> list[Declared]:
        return [d for d in self.declared if id(d.obj) not in self._superseded]

    def show(self, where) -> str:
        if where is None:
            return "?"
        path, line = where
        return f"{os.path.relpath(path, self.root)}:{line}"

    def declared_at(self, name, kind=None) -> list[Declared]:
        return [d for d in self.live() if d.name == name and (kind is None or d.kind == kind)]

    # -- the checks, once every conf has run ---------------------------------------------
    def check(self, project, *, strict=False) -> list[str]:
        """Raise on the first error; return the warnings (errors with `strict`)."""
        bound = _bound(project)
        bound_ids = {id(d) for d in bound.values()}
        parts_of_declared = {id(p) for d in self.live() if d.kind not in PARTS for p in _parts(d.obj)}
        self._collisions()
        for ref in self.references:
            if ref.kind in PARTS:
                continue  # checked where the slot is, against that server's own
            self._resolve(ref, bound, bound_ids)
        self._attributes(project, bound)
        unused = []
        named = {(r.kind, r.name) for r in self.references} | {
            ("*", m.group(1)) for text in _templates(project) for m in ATTRIBUTE_REF.finditer(text)
        }
        named_objects = {id(r.obj) for r in self.references if r.obj is not None}
        for d in self.live():
            if d.kind in PARTS:
                used = id(d.obj) in parts_of_declared or id(d.obj) in named_objects
            else:
                used = id(d.obj) in bound_ids or id(d.obj) in named_objects or {(d.kind, d.name), ("*", d.name)} & named
            if not used:
                unused.append(
                    f"{self.show(d.where)}: {d.kind} {d.name!r} is declared and not used; "
                    f"bind it in {BIND_IN.get(d.kind, 'the project')}, or remove it"
                )
        if unused and strict:
            raise UsageError("--strict: declarations that nothing uses:\n  " + "\n  ".join(unused))
        return unused

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

    def _resolve(self, ref, bound, bound_ids):
        at = self.show(ref.where)
        if ref.obj is not None:
            if id(ref.obj) not in bound_ids:
                d = next((d for d in self.declared if d.obj is ref.obj), None)
                made = f" (declared at {self.show(d.where)})" if d else ""
                raise UsageError(f"{at}: {ref.kind}={ref.name!r}{made} is not bound; add it to {BIND_IN[ref.kind]}")
            return
        target = bound.get(ref.name)
        if target is not None and kind_of(target) == ref.kind:
            return
        if target is not None:
            raise UsageError(f"{at}: {ref.kind}={ref.name!r} names a {kind_of(target)}, not a {ref.kind}")
        if found := self.declared_at(ref.name):
            d = found[0]
            if d.kind != ref.kind:
                raise UsageError(f"{at}: {ref.kind}={ref.name!r} names a {d.kind}, not a {ref.kind}")
            raise UsageError(
                f"{at}: {ref.kind}={ref.name!r} (declared at {self.show(d.where)}) is not bound; "
                f"add it to {BIND_IN[ref.kind]}"
            )
        known = sorted(n for n, d in bound.items() if kind_of(d) == ref.kind)
        raise UsageError(f"{at}: {ref.kind}={ref.name!r} is not declared{_did_you_mean(ref.name, known)}")

    def _attributes(self, project, bound):
        for where, text in _templates(project, labelled=True):
            for m in ATTRIBUTE_REF.finditer(text):
                name, attribute = m.groups()
                target = bound.get(name)
                if target is None:
                    if found := self.declared_at(name):
                        raise UsageError(
                            f"{where}: {m.group()}: {found[0].kind} {name!r} (declared at "
                            f"{self.show(found[0].where)}) is not bound; add it to {BIND_IN.get(found[0].kind)}"
                        )
                    raise UsageError(
                        f"{where}: {m.group()}: no resource is named {name!r}{_did_you_mean(name, sorted(bound))}"
                    )
                known = target.attribute_names()
                if known is not None and attribute not in known:
                    raise UsageError(
                        f"{where}: {m.group()}: a {kind_of(target)} has no attribute {attribute!r}; "
                        f"{name} has {', '.join(known) or 'none'}"
                    )


def _parts(obj):
    parts = getattr(obj, "parts", None)
    return parts() if callable(parts) else ()


def _bound(project) -> dict:
    """name -> declaration, for everything the loaded project binds."""
    out = {}
    for group in (project.engines, project.services, project.storages, project.credentials):
        out.update(group)
    return out


def _templates(project, labelled=False):
    """Every string the run fills with `{name.attr}`: the values of the rules and matrix values, and the
    templates of the storages."""
    from .declare import CONF_FILE

    out = []
    for conf_dir, rules in project._rules_by_dir.items():
        conf = os.path.relpath(os.path.join(conf_dir, CONF_FILE), project.root)
        for rule in rules:
            for key, value in rule.options.given.get("values", {}).items():
                if isinstance(value, str):
                    out.append((f"{conf}: {rule.label}: values[{key!r}]", value))
    for parameter, values in project.matrix.items():
        for name, options in values.items():
            for key, value in options.given.get("values", {}).items():
                if isinstance(value, str):
                    out.append((f"matrix[{parameter!r}][{name!r}]: values[{key!r}]", value))
    for storage in project.storages.values():
        for attr in ("root", "endpoint", "account"):
            value = getattr(storage, attr, None)
            if isinstance(value, str):
                out.append((f"storage {storage.name!r}: {attr}", value))
    return out if labelled else [text for _, text in out]


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


def ref(value, kind, where):
    """What a reference slot gives on: the name of `value`, a plain string. `value` is a name or the
    object; either is recorded for the checks at the end of the load."""
    names = _current.get()
    if isinstance(value, str):
        if names is not None:
            names.refer(kind, value)
        return value
    if kind_of(value) == kind:
        if names is not None:
            names.refer(kind, value.name, value)
        return value.name
    name = getattr(value, "name", None)
    shown = f"{type(value).__name__}({name!r})" if isinstance(name, str) else repr(value)
    raise UsageError(f"{where}: {kind} {shown}; give the name of a {kind}, or the {kind} itself")
