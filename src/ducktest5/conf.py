"""`ducktestconf.py`: configuration for a directory and everything below it.

The file at the root of the scanned tree is the project file; it may also declare ENGINES, SERVICES,
AXES, SETS and EXCLUDE. Any directory may have its own file with RULES. A rule speaks about the whole
subtree (`default`) or about a pattern relative to its file (`match`); a pattern may name one file.

How rules combine, in one order: the root file first, then each file on the way down to the test; in a
file, the rules as listed; then the test's own `@test(...)`. Along that order, list fields (tags,
services, engines, variants) add up, dict fields (values, settings) update by key, and single values
(in_default_set, timeout) are replaced. `why` records which rule gave what.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import sys
from dataclasses import dataclass, field

from .binary import UsageError
from .declare import CONF_FILE, Engine, Service, Variant

_LISTS = ("tags", "services", "engines", "variants")
_DICTS = ("values", "settings")
_SINGLES = ("in_default_set", "timeout")


@dataclass(frozen=True)
class Rule:
    pattern: str
    fields: dict

    def matches(self, rel_to_conf):
        p = self.pattern
        return (
            fnmatch.fnmatchcase(rel_to_conf, p) or (p.endswith("/**") and rel_to_conf.startswith(p[:-2])) or p == "**"
        )


def _rule(pattern, fields):
    unknown = sorted(set(fields) - set(_LISTS + _DICTS + _SINGLES))
    if unknown:
        raise UsageError(f"rule {pattern!r}: unknown field(s) {unknown}; known: {_LISTS + _DICTS + _SINGLES}")
    return Rule(pattern, fields)


def default(**fields):
    """A rule for every test below this file."""
    return _rule("**", fields)


def match(pattern, **fields):
    """A rule for the tests whose path, relative to this file, matches `pattern` (`*` stays in one
    directory level only when the pattern has no `**`; `dir/**` is everything below `dir`)."""
    return _rule(pattern, fields)


@dataclass
class Settled:
    """What the rules gave one test, and where each part came from."""

    tags: list = field(default_factory=list)
    services: list = field(default_factory=list)
    engines: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    in_default_set: bool = True
    timeout: float | None = None
    why: list = field(default_factory=list)

    def apply(self, fields, source):
        for name, value in fields.items():
            if name in _LISTS:
                target = getattr(self, name)
                target += [v for v in value if v not in target]
            elif name in _DICTS:
                getattr(self, name).update(value)
            else:
                setattr(self, name, value)
            self.why.append(f"{name}={value!r}  <- {source}")


@dataclass
class Project:
    root: str
    engines: dict
    services: dict
    axes: dict
    sets: dict
    exclude: tuple
    _rules_by_dir: dict = field(default_factory=dict)
    unused_rules: list = field(default_factory=list)

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
    """The directory of the highest `ducktestconf.py` at or above `start`, else `start` itself."""
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
    project._used = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")))
        if CONF_FILE not in filenames:
            continue
        path = os.path.join(dirpath, CONF_FILE)
        module = _load(path, "ducktestconf_" + os.path.relpath(dirpath, root).replace(os.sep, "_").replace(".", "root"))
        rules = list(getattr(module, "RULES", ()))
        if not all(isinstance(r, Rule) for r in rules):
            raise UsageError(f"{path}: RULES must hold default(...) and match(...) rules")
        project._rules_by_dir[dirpath] = rules
        if dirpath == root:
            project.engines = {e.id: e for e in getattr(module, "ENGINES", ())}
            project.services = {s.id: s for s in getattr(module, "SERVICES", ())}
            project.axes = {axis: {v.id: v for v in values} for axis, values in getattr(module, "AXES", {}).items()}
            project.sets = dict(getattr(module, "SETS", {}))
            project.exclude = tuple(getattr(module, "EXCLUDE", ()))
            _check(path, project)
        else:
            for name in ("ENGINES", "SERVICES", "AXES", "SETS", "EXCLUDE"):
                if hasattr(module, name):
                    raise UsageError(f"{path}: {name} belongs in the root {CONF_FILE} only; this file may hold RULES")
    return project


def _check(path, project):
    if not all(isinstance(e, Engine) for e in project.engines.values()):
        raise UsageError(f"{path}: ENGINES must hold Engine")
    if not all(isinstance(s, Service) for s in project.services.values()):
        raise UsageError(f"{path}: SERVICES must hold Service")
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
