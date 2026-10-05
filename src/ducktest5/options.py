"""The one set of options a test is given, in every place that gives one: a rule (`default()`,
`match()`), `@test(...)` and a value of a MATRIX parameter. Each place takes the same names (the
slots) and `Settled.apply` merges them all, in one order: the root `ducktest_conf.py`, each file
down to the test, then `@test`, then the MATRIX value of each parameter of the cell.

    tags, engines, matrix, mutex, requires          lists; they add up
    values, settings, process_env                    dicts; a later key replaces an earlier one
    init_sql                                         statements; they add up, in order
    storage, credential, service                     the id of one input of that kind; a later one replaces
    timeout, retries                                 a later one replaces

`matrix` names the parameters a test runs over, so it is not an option of a MATRIX value. A
cell's values give way to nothing: a value or a binding one gives that a rule, `@test` or another
parameter of the cell gives too is an error naming both; only what a `default()` binds gives way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields

from .binary import UsageError
from .provision import MASK, SECRET_NAME
from .resources.client import check_names

LISTS = ("tags", "engines", "matrix", "mutex", "requires")
DICTS = ("values", "settings", "process_env")
BINDINGS = ("storage", "credential", "service")
SINGLES = ("timeout", "retries")
NAMES = LISTS + DICTS + ("init_sql",) + BINDINGS + SINGLES


# Keys a `--select` term takes besides a parameter's name.
RESERVED = ("tag", "type", "path", "suite")
_BAD_VALUE_NAME = re.compile(r"[\s,;=\[\]]")


class Local:
    """A parameter that a rule or `@test` defines in its own `matrix=`: its values are free data for
    the body, not slots. Equal only to itself."""

    def __init__(self, name, values):
        self.name, self.values = name, values

    def __repr__(self):
        return repr({self.name: list(self.values)})


def check_parameter(name, values, where):
    """A parameter's name and the names of its values, as an id and a `--select` term spell them."""
    if not isinstance(name, str) or not name.isidentifier() or name in RESERVED:
        raise UsageError(
            f"{where}: parameter {name!r}; a parameter name is a Python identifier and not one of {list(RESERVED)}"
        )
    if not isinstance(values, dict) or not values:
        raise UsageError(f"{where}: parameter {name!r} must be a non-empty dict of value name -> value")
    for value in values:
        if not isinstance(value, str) or not value or _BAD_VALUE_NAME.search(value):
            raise UsageError(
                f"{where}: parameter {name!r} has value name {value!r}; a value name is a non-empty string "
                "without whitespace or any of , ; = [ ]"
            )


def _matrix(items, where):
    if isinstance(items, (str, dict)):
        raise UsageError(f"{where}: matrix={items!r}; it is a list")
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for name, values in item.items():
                check_parameter(name, values, f"{where}: matrix")
                out.append(Local(name, dict(values)))
        else:
            raise UsageError(
                f"{where}: matrix item {item!r}; it is the name of a MATRIX parameter or a dict of parameters"
            )
    return tuple(out)


def parameter_name(item):
    return item if isinstance(item, str) else item.name


def _masked(given):
    return {k: MASK if v is not None and SECRET_NAME.search(k) else v for k, v in given.items()}


def from_matrix(source):
    """Whether a source, as `Settled` records it, is a value of a cell."""
    return source.lower().startswith("matrix ")


def is_default(source):
    """Whether a source, as `Settled` records it, is a `default()`."""
    return source.endswith(": default")


@dataclass(frozen=True)
class Options:
    """What one place gives, as written. `given` holds only the names it was given."""

    given: dict

    @classmethod
    def of(cls, given, where, *, leave_out=()):
        """Checks the names and the shape of each; `leave_out` names what this place does not take."""
        known = [n for n in NAMES if n not in leave_out]
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise UsageError(f"{where}: unknown option(s) {unknown}; it takes {known}")
        out = {}
        for name, value in given.items():
            if value is None:
                continue
            if name == "matrix":
                value = _matrix(value, where)
            elif name == "mutex" or name == "init_sql":
                value = (value,) if isinstance(value, str) else tuple(value)
            elif name in LISTS:
                if isinstance(value, str):
                    raise UsageError(f"{where}: {name}={value!r}; it is a list")
                value = tuple(value)
            elif name in DICTS:
                value = dict(check_names(dict(value), f"{where}: {name}"))
            elif name in BINDINGS and not isinstance(value, str):
                raise UsageError(f"{where}: {name}={value!r}; it is the id of one, a string")
            elif name == "retries" and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise UsageError(f"{where}: retries={value!r}; it is a whole number, 0 or more")
            out[name] = value
        return cls(out)


@dataclass(kw_only=True)
class Settled:
    """What the places gave one test, merged, and where each part came from."""

    tags: list = field(default_factory=list)
    engines: list = field(default_factory=list)  # ids of the engines it drives, among the project's ENGINES
    matrix: list = field(default_factory=list)  # parameter names and Local parameters, in the order given
    mutex: list = field(default_factory=list)  # what it must have to itself while it runs
    requires: list = field(default_factory=list)  # extensions loaded before its settings
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    process_env: dict = field(default_factory=dict)  # a value sets a variable, None unsets it
    init_sql: list = field(default_factory=list)
    bind: dict = field(default_factory=dict)  # storage/credential/service -> the id it binds
    timeout: float | None = None
    retries: int = 0  # how often a FAILED or ERROR may run again
    why: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)  # (field, key) -> the place that gave that one entry

    def source_of(self, name, key):
        """Which place gave one entry; what no rule gave, the definition declared itself."""
        return self.sources.get((name, key), "@test")

    def settled(self):
        """A copy of the merged options alone, to merge more onto."""
        out = Settled()
        for f in fields(Settled):
            value = getattr(self, f.name)
            setattr(out, f.name, type(value)(value) if isinstance(value, (list, dict)) else value)
        return out

    def apply(self, options, source, *, cell=None):
        """Merge what one place gave. `cell` is the id of the test when the place is a value of a
        cell, which gives way to nothing."""
        for name, value in options.given.items():
            if name == "init_sql":
                for sql in value:
                    self.sources[("init_sql", len(self.init_sql))] = source
                    self.init_sql.append(sql)
                self.why.append(f"init_sql  <- {source}")  # it may hold a secret: named by its source only
                continue
            elif name == "matrix":
                for v in value:
                    self._add_parameter(v, source)
                value = list(value)
            elif name in LISTS:
                target = getattr(self, name)
                for v in value:
                    if v not in target:
                        target.append(v)
                        self.sources[(name, v)] = source
                value = list(value)
            elif name == "values":
                for k, v in value.items():
                    self.give_value(k, v, source, cell=cell)
                value = _masked(value)
            elif name in DICTS:
                getattr(self, name).update(value)
                self.sources.update({(name, k): source for k in value})
                value = _masked(value)
            elif name in BINDINGS:
                self._bind(name, value, source, cell)
            else:
                setattr(self, name, value)
            self.why.append(f"{name}={value!r}  <- {source}")

    def _add_parameter(self, item, source):
        """A parameter appears once; two places may name one MATRIX parameter, but no other two
        parameters may share a name."""
        name = parameter_name(item)
        given = next((p for p in self.matrix if parameter_name(p) == name), None)
        if given is None:
            self.matrix.append(item)
            self.sources[("matrix", name)] = source
        elif given is not item and not (isinstance(item, str) and isinstance(given, str)):
            raise UsageError(
                f"{source}: matrix parameter {name!r} is given by {self.sources[('matrix', name)]} too; "
                "a parameter has one name"
            )

    def give_value(self, name, value, source, *, cell=None, where=None):
        """Names are case-insensitive: a later place's `PATH` replaces an earlier one's `path`, but
        a cell's value replaces nothing. `where` names the place in an error, if not `source`."""
        where = where or source
        for given in [k for k in self.values if k.lower() == name.lower()]:
            if cell is not None:
                earlier = self.sources.get(("cell_value", given.lower())) or self.source_of("values", given)
                if from_matrix(earlier):
                    raise UsageError(f"{cell}: {name!r} is given by both {earlier} and {where}")
                raise UsageError(
                    f"{cell}: the value {name!r} is given by {where} and by {earlier}; give it in one place"
                )
            del self.values[given]
            self.sources.pop(("values", given), None)
        self.values[name] = value
        self.sources[("values", name)] = source
        if cell is not None:
            self.sources[("cell_value", name.lower())] = where

    def _bind(self, kind, ref, source, cell):
        if cell is not None and kind in self.bind:
            earlier = self.source_of("bind", kind)
            if from_matrix(earlier):
                raise UsageError(f"{cell}: {kind}= is given by {earlier} and by {source}; bind it in one place")
            if not is_default(earlier):
                raise UsageError(
                    f"{cell}: {kind}= is {self.bind[kind]!r} by {earlier} and {ref!r} by {source}; bind it in one place"
                )
        self.bind[kind] = ref
        self.sources[("bind", kind)] = source
