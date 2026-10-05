"""`ducktest_conf.py`: configuration for a directory and everything below it.

The file at the root of the scanned tree is the project file; it may also declare ENGINES, SERVICES,
STORAGES, CREDENTIALS, AXES, SUITES, EXCLUDE, PYTHONPATH and INHERIT_ENV.

`INHERIT_ENV = ["AWS_CA_BUNDLE"]` names variables engine processes keep from the shell's env. Every
other name under an SDK prefix (`AWS_`) is left out of them: an SDK reads it with getenv(), so a test
that gets it from the shell can pass with one person's setup and fail elsewhere. Keys come from a
credential, never from here.

`PYTHONPATH = ["py", "../scripts"]` puts the repo's helper code on `sys.path`, each path relative to
the project file, before the project file itself runs: it is read from the file's source, so it must
be a literal list of strings, and the file may then import from it. It holds for the whole process,
so every body, wherever and whenever it imports a helper, sees it. Engine processes do not get it. Any directory may have its own file with RULES. A rule
speaks about the whole subtree (`default`) or about a pattern relative to its file (`match`); a
pattern may name one file.

How rules combine, in one order: the root file first, then each file on the way down to the test definition;
in a file, the rules as listed; then the definition's own `@test(...)`. Along that order, list fields (tags,
engines, variants, mutex, suites, requires) add up, dict fields (values, settings, bind) update by key, and
single values (timeout, retries) are replaced. `why` records which rule gave what. All of them are on
the definition and apply to every test made from it.

`requires=["httpfs"]` loads an extension before anything else, as a Variant's does: a setting of it in
`settings` does not exist before.

`bind={"storage": "scratch"}` binds a role to one of STORAGES, CREDENTIALS or SERVICES by id. The role
is the key the merge updates by: a later binding of the same role replaces the earlier one, so a
test's own `@test(bind=...)` wins over its rules. An axis value's inputs bind too, each the role its
kind names (`storage`, `credential`): one replaces what a `default()` binds, a fallback; a role that
a `match()` or `@test` binds as well is an error, not a silent winner.

ENGINES holds `EngineDef`s (`duckdb_engine(...)`, `delta_spark(...)`, ...); `engines=["duck"]` names
the engines a test drives, each through a Client of its own. SERVICES holds the project's other
`ServiceDef`s of `resources`. An engine stays a kind of service internally -- one lifecycle, one
provisioning path -- so a service that is an input of a storage, a credential or an engine is one
of the project's services too, whether SERVICES names it or not.

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

import ast
import functools
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass, field, replace

from .binary import UsageError
from .declare import CONF_FILE, Variant, as_mutex
from .resources import CredentialDef, DefaultStorage, EngineDef, ResourceDef, ServiceDef, StorageDef
from .resources.client import check_names
from .resources.credential import inherit_env
from .resources.made import services_in

_log = logging.getLogger(__name__)

_LISTS = ("tags", "engines", "variants", "mutex", "suites", "requires")
_DICTS = ("values", "settings", "bind")
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


def is_default(source):
    """Whether a rule's source, as `Settled` records it, is a `default()`."""
    return source.endswith(": default")


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
    engines: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    mutex: list = field(default_factory=list)
    suites: list = field(default_factory=list)
    requires: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    bind: dict = field(default_factory=dict)
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
            elif name == "values":
                # Names are case-insensitive: a later rule's `PATH` replaces an earlier one's `path`.
                given = {k.lower() for k in check_names(value, source or "@test")}
                for k in [k for k in self.values if k.lower() in given]:
                    del self.values[k]
                    self.sources.pop((name, k), None)
                self.values.update(value)
                self.sources.update({(name, k): source for k in value})
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
    storages: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    _rules_by_dir: dict = field(default_factory=dict)
    _used: set = field(default_factory=set)
    unused_rules: list = field(default_factory=list)
    default_storage: DefaultStorage | None = None  # None: `<repo>/data` and the default temp root
    pythonpath: tuple = ()  # absolute, in the order PYTHONPATH gave them
    inherit_env: tuple = ()  # names engines keep from the shell's env under an SDK prefix

    def __post_init__(self):
        if self.default_storage is None:
            self.default_storage = DefaultStorage(os.path.join(repo_root(self.root), "data"))

    def inputs(self):
        """What a test may bind or a variant may name as an input, by id."""
        return {**self.services, **self.storages, **self.credentials}

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
        if parent == here or _has_git(here):
            return found or os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
        here = parent


def _has_git(path):
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def repo_root(project_root):
    """The top of the git repository the project is in, else the project root."""
    here = os.path.abspath(project_root)
    while not _has_git(here):
        if os.path.dirname(here) == here:
            return os.path.abspath(project_root)
        here = os.path.dirname(here)
    return here


def _literal_pythonpath(path, root):
    """PYTHONPATH of the project file, as absolute directories, read from its source without running it."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    found = None
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "PYTHONPATH" for t in targets):
            found = node
    if found is None or found.value is None:
        return ()
    try:
        value = ast.literal_eval(found.value)
    except ValueError:
        value = None
    if not isinstance(value, (list, tuple)) or not all(isinstance(p, str) and p for p in value):
        raise UsageError(
            f"{path}:{found.lineno}: PYTHONPATH must be a literal list of paths, read before the file runs: "
            'PYTHONPATH = ["py", "../scripts"]'
        )
    top, out = repo_root(root), []
    for p in value:
        full = os.path.normpath(os.path.join(root, p))
        if os.path.isabs(p) or os.path.commonpath([top, full]) != top:
            raise UsageError(
                f"{path}: PYTHONPATH entry {p!r} is outside the repository {top}; give a path relative to {root}"
            )
        if not os.path.isdir(full):
            raise UsageError(f"{path}: PYTHONPATH entry {p!r} is not a directory ({full})")
        out.append(full)
    return tuple(out)


def _apply_pythonpath(paths):
    for p in reversed(paths):
        if p not in sys.path:
            sys.path.insert(0, p)


def load_project(start):
    root = find_root(start)
    project = Project(root, {}, {}, {}, {}, ())
    inherit_env(())
    root_file = os.path.join(root, CONF_FILE)
    if os.path.isfile(root_file):
        project.pythonpath = _literal_pythonpath(root_file, root)
        _apply_pythonpath(project.pythonpath)
    for dirpath, dirnames, filenames in os.walk(root):  # top-down, so the root file and its EXCLUDE come first
        for name in filenames:
            if _misnamed_conf(name):
                _log.warning("%s: not read; the file ducktest5 reads is %s", os.path.join(dirpath, name), CONF_FILE)
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
            project.storages = {s.id: s for s in getattr(module, "STORAGES", ())}
            project.credentials = {c.id: c for c in getattr(module, "CREDENTIALS", ())}
            project.axes = {axis: {v.id: v for v in values} for axis, values in getattr(module, "AXES", {}).items()}
            project.suites = dict(getattr(module, "SUITES", {}))
            if "all" in project.suites:
                raise UsageError(f"{path}: a project may not define a suite named 'all' -- it always exists, built in")
            project.exclude = tuple(getattr(module, "EXCLUDE", ()))
            project.inherit_env = inherit_env(list(getattr(module, "INHERIT_ENV", ())), f"{path}: INHERIT_ENV")
            if (
                hasattr(module, "PYTHONPATH")
                and tuple(os.path.normpath(os.path.join(root, p)) for p in module.PYTHONPATH) != project.pythonpath
            ):
                raise UsageError(
                    f"{path}: PYTHONPATH is read from the source before the file runs, as one top-level literal; "
                    "it may not be computed or changed as the file runs"
                )
            _check(path, project)
        else:
            for name in (
                "ENGINES",
                "SERVICES",
                "STORAGES",
                "CREDENTIALS",
                "AXES",
                "SUITES",
                "EXCLUDE",
                "PYTHONPATH",
                "INHERIT_ENV",
            ):
                if hasattr(module, name):
                    raise UsageError(f"{path}: {name} belongs in the root {CONF_FILE} only; this file may hold RULES")
        _prune(project, root, dirpath, dirnames)
    return project


def _misnamed_conf(name):
    """A name meant as `ducktest_conf.py` and not it: `ducktest_config.py`, `ducktest-conf.py`, the legacy
    `ducktestconf.py`, another case. It warns and does not fail: the file may be a helper of the repo's own
    under that name, and a run must not break on a name it never reads."""
    stem, ext = os.path.splitext(name)
    return (
        name != CONF_FILE
        and ext.lower() == ".py"
        and re.sub(r"[-_]", "", stem.lower()) in ("ducktestconf", "ducktestconfig")
    )


def _prune(project, root, dirpath, dirnames):
    rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
    dirnames[:] = sorted(
        d
        for d in dirnames
        if not d.startswith((".", "__")) and not project.excludes(d if rel_dir == "." else f"{rel_dir}/{d}")
    )


def _check(path, project):
    for name, kind, declared in (
        ("STORAGES", StorageDef, project.storages),
        ("CREDENTIALS", CredentialDef, project.credentials),
    ):
        if not all(isinstance(d, kind) for d in declared.values()):
            raise UsageError(f"{path}: {name} must hold {kind.__name__}")
    for storage in project.storages.values():
        for _, _, service in storage.edges():
            if isinstance(service, ServiceDef):
                service.check_storage(storage)
    for c in project.credentials.values():
        for storage_id in c.scope_storages:
            storage = project.storages.get(storage_id)
            scheme = storage.root.split("://", 1)[0].lower() if storage and "://" in storage.root else "file"
            # A scheme a template gives is checked where the storage is made.
            if storage is not None and "{" not in scheme and scheme not in type(c).schemes:
                raise UsageError(
                    f"{path}: {c.label} is scoped to storage {storage_id!r} ({storage.root}): an "
                    f"{type(c).__name__} is for {', '.join(type(c).schemes)} storages only"
                )
    bad_engine = next((e for e in project.engines.values() if not isinstance(e, EngineDef)), None)
    if bad_engine is not None:
        raise UsageError(f"{path}: {bad_engine!r} is not an engine; declare services in SERVICES")
    bad_service = next((s for s in project.services.values() if isinstance(s, EngineDef)), None)
    if bad_service is not None:
        raise UsageError(f"{path}: {bad_service!r} is an engine; declare engines in ENGINES")
    if not all(isinstance(s, ServiceDef) for s in project.services.values()):
        raise UsageError(f"{path}: SERVICES must hold ServiceDef of ducktest5.resources")
    for s in services_in([*project.storages.values(), *project.credentials.values(), *project.engines.values()]):
        if project.services.setdefault(s.id, s) is not s:
            raise UsageError(f"{path}: two services are named {s.id!r}; a service is one declaration")
    for axis, values in project.axes.items():
        if not values or not all(isinstance(v, Variant) for v in values.values()):
            raise UsageError(f"{path}: AXES[{axis!r}] must be a non-empty list of Variant")
        for v in values.values():
            unknown = [x for x in v.engines if x not in project.engines]
            if unknown:
                raise UsageError(f"{path}: AXES[{axis!r}] value {v.id!r} names engines {unknown} that are not declared")
            values[v.id] = replace(v, inputs=_bound(path, f"AXES[{axis!r}] value {v.id!r}", v.inputs, project))


def _bound(path, where, inputs, project):
    """A variant's inputs with each id replaced by the Def it names, as `({Def: mode}, ...)`."""
    declared = project.inputs()
    out = []
    for entry in inputs:
        for ref, mode in entry.items() if isinstance(entry, dict) else [(entry, "rw")]:
            if not isinstance(ref, ResourceDef) and ref not in declared:
                raise UsageError(
                    f"{path}: {where} names input {ref!r}, which is not in STORAGES, CREDENTIALS or SERVICES"
                )
            out.append({declared[ref] if isinstance(ref, str) else ref: mode})
    return tuple(out)
