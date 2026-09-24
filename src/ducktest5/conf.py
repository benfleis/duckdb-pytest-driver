"""`ducktest_conf.py`: configuration for a directory and everything below it.

The file at the root of the scanned tree is the project file; it may also declare ENGINES, SERVICES,
AXES, SUITES and EXCLUDE. Any directory may have its own file with RULES. A rule speaks about the whole
subtree (`default`) or about a pattern relative to its file (`match`); a pattern may name one file.

How rules combine, in one order: the root file first, then each file on the way down to the test definition;
in a file, the rules as listed; then the definition's own `@test(...)`. Along that order, list fields (tags,
services, engines, variants, mutex, suites) add up, dict fields (values, settings) update by key, and
single values (timeout, retries) are replaced. `why` records which rule gave what. All of them are on
the definition and apply to every test made from it.

`mutex="name"`, or several names, says what a test must have to itself: no two tests that name one of
them run at the same time, however many workers the run has.

`suites` names the suites a test is in: `"smoke"` declares it in, `"!smoke"` declares it out. It adds up
like the other list fields, except that a name may be said again: the last word on a name wins, in the
place that name was first said. `!all` is an error -- every test is in the suite `all`.

`retries=N` lets a test that ended FAILED or ERROR run again, alone, up to N times; it is PASSED when an
attempt passes, and the report says which attempt. It is for a test that can fail for reasons that are
not its own, such as one that reaches the public network.

`timeout=N` bounds one test, in seconds, whichever way it runs -- a `.test` file inside the binary, or a
Python body in its worker thread -- counted from when it begins. Past it the test ends ERROR; a Python
body's thread cannot be killed, so it is only given up on, not stopped.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field

from .binary import UsageError
from .declare import CONF_FILE, EngineDef, ServiceDef, Variant, as_mutex

_LISTS = ("tags", "services", "engines", "variants", "mutex", "suites")
_DICTS = ("values", "settings")
_SINGLES = ("timeout", "retries")


def suite_name(entry):
    """The suite a `suites` entry speaks about: `!smoke` and `smoke` both name `smoke`."""
    return entry[1:] if entry.startswith("!") else entry


def merge_suites(target, entries, source, sources):
    """Adds `entries` to `target`, the last word on a name winning where the name was first said.
    `source` is what said them, or None for the definition's own `@test(...)`."""
    for entry in entries:
        at = next((i for i, e in enumerate(target) if suite_name(e) == suite_name(entry)), None)
        if at is None:
            target.append(entry)
        else:
            sources.pop(("suites", target[at]), None)
            target[at] = entry
        sources.pop(("suites", entry), None)
        if source is not None:
            sources[("suites", entry)] = source


def check_suites(entries, where):
    for entry in entries:
        if suite_name(entry) == "all" and entry.startswith("!"):
            raise UsageError(f"{where}: '!all' -- every test is in the suite 'all'")


@dataclass(frozen=True)
class Rule:
    pattern: str
    fields: dict

    def matches(self, rel_to_conf):
        return _glob(self.pattern).fullmatch(rel_to_conf) is not None


@functools.lru_cache(maxsize=None)
def _glob(pattern):
    """`*` and `?` stay inside one path segment; `**` crosses segments; `dir/**` is everything below
    `dir`. `fnmatch` alone lets `*` cross `/`, so `*.test` would also match `sub/deep/x.test`."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def _rule(pattern, fields):
    unknown = sorted(set(fields) - set(_LISTS + _DICTS + _SINGLES))
    if unknown:
        raise UsageError(f"rule {pattern!r}: unknown field(s) {unknown}; known: {_LISTS + _DICTS + _SINGLES}")
    if "mutex" in fields:
        fields = {**fields, "mutex": as_mutex(fields["mutex"])}
    retries = fields.get("retries", 0)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise UsageError(f"rule {pattern!r}: retries={retries!r}; it is a whole number, 0 or more")
    check_suites(fields.get("suites", ()), f"rule {pattern!r}")
    return Rule(pattern, fields)


def default(**fields):
    """A rule for every test definition below this file."""
    return _rule("**", fields)


def match(pattern, **fields):
    """A rule for the test definitions whose path, relative to this file, matches `pattern`: `*` stays inside one
    directory level, `**` crosses levels, `dir/**` is everything below `dir`, and a pattern with no
    wildcard names one file."""
    return _rule(pattern, fields)


@dataclass
class Settled:
    """What the rules gave one test definition, and where each part came from."""

    tags: list = field(default_factory=list)
    services: list = field(default_factory=list)
    engines: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    mutex: list = field(default_factory=list)
    suites: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    timeout: float | None = None
    retries: int = 0
    why: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)  # (field, key) -> the rule that gave that one entry

    def apply(self, fields, source):
        for name, value in fields.items():
            if name == "suites":
                merge_suites(self.suites, value, source, self.sources)
            elif name in _LISTS:
                target = getattr(self, name)
                for v in value:
                    if v not in target:
                        target.append(v)
                        self.sources[(name, v)] = source
            elif name in _DICTS:
                getattr(self, name).update(value)
                self.sources.update({(name, k): source for k in value})
            else:
                setattr(self, name, value)
            self.why.append(f"{name}={value!r}  <- {source}")


@dataclass
class Project:
    root: str
    engines: dict
    services: dict
    axes: dict
    suites: dict
    exclude: tuple
    _rules_by_dir: dict = field(default_factory=dict)
    _used: set = field(default_factory=set)
    unused_rules: list = field(default_factory=list)

    def excludes(self, rel_to_root):
        return any(
            _glob(p).fullmatch(rel_to_root) or _glob(p.rstrip("/") + "/**").fullmatch(rel_to_root) for p in self.exclude
        )

    def excluded_bodies(self, working_dir):
        """The `.test` files EXCLUDE leaves out, relative to `working_dir`: the binary still knows them."""
        out = []
        for dirpath, _, filenames in os.walk(self.root):
            for f in filenames:
                rel = os.path.relpath(os.path.join(dirpath, f), self.root).replace(os.sep, "/")
                if f.endswith((".test", ".test_slow", ".test_coverage")) and self.excludes(rel):
                    out.append(os.path.relpath(os.path.join(dirpath, f), working_dir))
        return out

    def settle(self, test_path):
        """Apply every rule on the way from the root down to `test_path` (absolute)."""
        settled = Settled()
        here = os.path.dirname(test_path)
        chain = []
        while True:
            if here in self._rules_by_dir:
                chain.append(here)
            if here == self.root or os.path.dirname(here) == here:
                break
            here = os.path.dirname(here)
        for conf_dir in reversed(chain):
            rel = os.path.relpath(test_path, conf_dir).replace(os.sep, "/")
            for n, rule in enumerate(self._rules_by_dir[conf_dir]):
                if rule.matches(rel):
                    self._used.add((conf_dir, n))
                    where = os.path.relpath(os.path.join(conf_dir, CONF_FILE), self.root)
                    settled.apply(
                        rule.fields, f"{where}: {'default' if rule.pattern == '**' else 'match ' + repr(rule.pattern)}"
                    )
        return settled

    def note_unused_rules(self):
        for conf_dir, rules in self._rules_by_dir.items():
            for n, rule in enumerate(rules):
                if (conf_dir, n) not in self._used and rule.pattern != "**":
                    where = os.path.relpath(os.path.join(conf_dir, CONF_FILE), self.root)
                    self.unused_rules.append(f"{where}: match {rule.pattern!r} matched no test")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def find_root(start):
    """The directory of the highest `ducktest_conf.py` at or above `start`, else `start` itself."""
    here = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
    found = None
    while True:
        if os.path.isfile(os.path.join(here, CONF_FILE)):
            found = here
        parent = os.path.dirname(here)
        if parent == here or os.path.isdir(os.path.join(here, ".git")) or os.path.isfile(os.path.join(here, ".git")):
            return found or os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
        here = parent


def load_project(start):
    root = find_root(start)
    project = Project(root, {}, {}, {}, {}, ())
    for dirpath, dirnames, filenames in os.walk(root):  # top-down, so the root file and its EXCLUDE come first
        if CONF_FILE not in filenames:
            _prune(project, root, dirpath, dirnames)
            continue
        path = os.path.join(dirpath, CONF_FILE)
        module = _load(
            path, "ducktest_conf_" + os.path.relpath(dirpath, root).replace(os.sep, "_").replace(".", "root")
        )
        rules = list(getattr(module, "RULES", ()))
        if not all(isinstance(r, Rule) for r in rules):
            raise UsageError(f"{path}: RULES must hold default(...) and match(...) rules")
        project._rules_by_dir[dirpath] = rules
        if dirpath == root:
            project.engines = {e.id: e for e in getattr(module, "ENGINES", ())}
            project.services = {s.id: s for s in getattr(module, "SERVICES", ())}
            project.axes = {axis: {v.id: v for v in values} for axis, values in getattr(module, "AXES", {}).items()}
            project.suites = dict(getattr(module, "SUITES", {}))
            if "all" in project.suites:
                raise UsageError(f"{path}: a project may not define a suite named 'all' -- it always exists, built in")
            project.exclude = tuple(getattr(module, "EXCLUDE", ()))
            _check(path, project)
        else:
            for name in ("ENGINES", "SERVICES", "AXES", "SUITES", "EXCLUDE"):
                if hasattr(module, name):
                    raise UsageError(f"{path}: {name} belongs in the root {CONF_FILE} only; this file may hold RULES")
        _prune(project, root, dirpath, dirnames)
    return project


def _prune(project, root, dirpath, dirnames):
    rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
    dirnames[:] = sorted(
        d
        for d in dirnames
        if not d.startswith((".", "__")) and not project.excludes(d if rel_dir == "." else f"{rel_dir}/{d}")
    )


def _check(path, project):
    if not all(isinstance(e, EngineDef) for e in project.engines.values()):
        raise UsageError(f"{path}: ENGINES must hold EngineDef")
    if not all(isinstance(s, ServiceDef) for s in project.services.values()):
        raise UsageError(f"{path}: SERVICES must hold ServiceDef")
    for axis, values in project.axes.items():
        if not values or not all(isinstance(v, Variant) for v in values.values()):
            raise UsageError(f"{path}: AXES[{axis!r}] must be a non-empty list of Variant")
        for v in values.values():
            for kind, known in (("services", project.services), ("engines", project.engines)):
                unknown = [x for x in getattr(v, kind) if x not in known]
                if unknown:
                    raise UsageError(
                        f"{path}: AXES[{axis!r}] value {v.id!r} names {kind} {unknown} that are not declared"
                    )
