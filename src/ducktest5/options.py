"""The one set of options a test is given, in every place that gives one: a rule (`default()`,
`match()`), `@test(...)` and an axis value (`Variant(...)`). Each place takes the same names and
`Settled.apply` merges them all, in one order: the root `ducktest_conf.py`, each file down to the
test, then `@test`, then each axis value of the test in AXES order.

    tags, engines, axes, mutex, suites, requires    lists; they add up
    values, settings, process_env                    dicts; a later key replaces an earlier one
    init_sql                                         statements; they add up, in order
    storage, credential, service                     the id of one input of that kind; a later one replaces
    timeout, retries                                 a later one replaces

`axes` names the project's axes a test takes part in, so it is not an option of an axis value.
An axis value gives way to nothing: a value or a binding it gives that a rule, `@test`, the params
row or another axis value gives too is an error naming both; only what a `default()` binds gives way
to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from .binary import UsageError
from .provision import MASK, SECRET_NAME
from .resources.client import check_names

LISTS = ("tags", "engines", "axes", "mutex", "suites", "requires")
DICTS = ("values", "settings", "process_env")
BINDINGS = ("storage", "credential", "service")
SINGLES = ("timeout", "retries")
NAMES = LISTS + DICTS + ("init_sql",) + BINDINGS + SINGLES


def suite_name(entry):
    """The suite a `suites` entry speaks about: `!smoke` and `smoke` both name `smoke`."""
    return entry[1:] if entry.startswith("!") else entry


def check_suites(entries, where):
    for entry in entries:
        if suite_name(entry) == "all" and entry.startswith("!"):
            raise UsageError(f"{where}: '!all' -- every test is in the suite 'all'")


def merge_suites(target, entries, source, sources):
    """Adds `entries` to `target`, the last word on a name winning where the name was first said."""
    for entry in entries:
        at = next((i for i, e in enumerate(target) if suite_name(e) == suite_name(entry)), None)
        if at is None:
            target.append(entry)
        else:
            sources.pop(("suites", target[at]), None)
            target[at] = entry
        sources.pop(("suites", entry), None)
        sources[("suites", entry)] = source


def _masked(given):
    return {k: MASK if v is not None and SECRET_NAME.search(k) else v for k, v in given.items()}


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
            if name == "mutex" or name == "init_sql":
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
            if name == "suites":
                check_suites(value, where)
            out[name] = value
        return cls(out)


@dataclass(kw_only=True)
class Settled:
    """What the places gave one test, merged, and where each part came from."""

    tags: list = field(default_factory=list)
    engines: list = field(default_factory=list)  # ids of the engines it drives, among the project's ENGINES
    axes: list = field(default_factory=list)
    mutex: list = field(default_factory=list)  # what it must have to itself while it runs
    suites: list = field(default_factory=list)  # a suite it declares itself in, or out of as `!NAME`
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

    def apply(self, options, source, *, axis=None):
        """Merge what one place gave. `axis` is the id of the test when the place is an axis value,
        which gives way to nothing."""
        for name, value in options.given.items():
            if name == "suites":
                merge_suites(self.suites, value, source, self.sources)
            elif name == "init_sql":
                for sql in value:
                    self.sources[("init_sql", len(self.init_sql))] = source
                    self.init_sql.append(sql)
                self.why.append(f"init_sql  <- {source}")  # it may hold a secret: named by its source only
                continue
            elif name in LISTS:
                target = getattr(self, name)
                for v in value:
                    if v not in target:
                        target.append(v)
                        self.sources[(name, v)] = source
                value = list(value)
            elif name == "values":
                for k, v in value.items():
                    self.give_value(k, v, source, axis=axis)
                value = _masked(value)
            elif name in DICTS:
                getattr(self, name).update(value)
                self.sources.update({(name, k): source for k in value})
                value = _masked(value)
            elif name in BINDINGS:
                self._bind(name, value, source, axis)
            else:
                setattr(self, name, value)
            self.why.append(f"{name}={value!r}  <- {source}")

    def give_value(self, name, value, source, *, axis=None, where=None):
        """Names are case-insensitive: a later place's `PATH` replaces an earlier one's `path`, but
        an axis value replaces nothing. `where` names the place in an error, if not `source`."""
        where = where or source
        for given in [k for k in self.values if k.lower() == name.lower()]:
            if axis is not None:
                earlier = self.sources.get(("axis_value", given.lower())) or self.source_of("values", given)
                if earlier.startswith("AXES "):
                    raise UsageError(f"{axis}: {name!r} is given by both {earlier} and {where}")
                raise UsageError(
                    f"{axis}: the value {name!r} is given by {where} and by {earlier}; give it in one place"
                )
            del self.values[given]
            self.sources.pop(("values", given), None)
        self.values[name] = value
        self.sources[("values", name)] = source
        if axis is not None:
            self.sources[("axis_value", name.lower())] = where

    def _bind(self, kind, ref, source, axis):
        if axis is not None and kind in self.bind:
            earlier = self.source_of("bind", kind)
            if earlier.startswith("AXES "):
                raise UsageError(f"{axis}: {kind}= is given by {earlier} and by {source}; bind it in one place")
            if not is_default(earlier):
                raise UsageError(
                    f"{axis}: {kind}= is {self.bind[kind]!r} by {earlier} and {ref!r} by {source}; bind it in one place"
                )
        self.bind[kind] = ref
        self.sources[("bind", kind)] = source
