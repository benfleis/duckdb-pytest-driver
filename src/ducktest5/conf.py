"""`ducktest_conf.py`: configuration for a directory and everything below it.

The file at the root of the scanned tree is the project file; it may also declare ENGINES, SERVICES,
STORAGES, CREDENTIALS, AXES, SUITES, PYTHONPATH and INHERIT_ENV.

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

A rule takes the options `options.py` lists, as `@test(...)` and an axis value do, and they merge in
one order: the root file first, then each file on the way down to the test definition; in a file,
the rules as listed; then the definition's own `@test(...)`, then its axis values. `why` records
which place gave what.

ENGINES holds `EngineDef`s (`duckdb_engine(...)`, `delta_spark(...)`, ...); `engines=["duck"]` names
the engines a test drives, each through a Client of its own. SERVICES holds the project's other
`ServiceDef`s of `resources`. An engine stays a kind of service internally -- one lifecycle, one
provisioning path -- so a service that is an input of a storage, a credential or an engine is one
of the project's services too, whether SERVICES names it or not.

`mutex="name"`, or several names, says what a test must have to itself: no two tests that name one of
them run at the same time, however many workers the run has.

A test is in a suite by what it carries: its path, its tags, its axis values. `SUITES` names
selections over those (`select.py`); `tags=["opt-in"]` keeps a test out of a bare run.

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
from .declare import CONF_FILE, Variant
from .options import BINDINGS, Options, Settled
from .resources import CredentialDef, DefaultStorage, EngineDef, ServiceDef, StorageDef
from .resources.credential import inherit_env
from .resources.made import services_in

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    pattern: str
    options: Options
    requires_env: str | None = None

    def matches(self, rel_to_conf, body_file=None):
        if _glob(self.pattern).fullmatch(rel_to_conf) is None:
            return False
        return self.requires_env is None or (body_file is not None and self.requires_env in _env_gates(body_file))

    @property
    def label(self):
        if self.pattern == "**" and self.requires_env is None:
            return "default"
        return f"match {self.pattern!r}" + (f", requires_env={self.requires_env!r}" if self.requires_env else "")


@functools.lru_cache(maxsize=None)
def _env_gates(path):
    """The names a `.test` file gates itself on with `require-env NAME [VALUE]`, read on demand."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return frozenset(line.split()[1] for line in f if line.startswith("require-env") and len(line.split()) > 1)
    except OSError:
        return frozenset()


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
    return Rule(pattern, Options.of(fields, f"rule {pattern!r}"))


def default(**fields):
    """A rule for every test definition below this file."""
    return _rule("**", fields)


def match(pattern, *, requires_env=None, **fields):
    """A rule for the test definitions whose path, relative to this file, matches `pattern`: `*` stays inside one
    directory level, `**` crosses levels, `dir/**` is everything below `dir`, and a pattern with no
    wildcard names one file.

    `requires_env="NAME"` narrows it to the `.test` files that hold a `require-env NAME` line, with
    or without a value. Deprecated: a starting point for a repo that gates files with `require-env`;
    move to tags or paths."""
    if requires_env is not None and not isinstance(requires_env, str):
        raise UsageError(f"rule {pattern!r}: requires_env={requires_env!r}; it is one name")
    return replace(_rule(pattern, fields), requires_env=requires_env)


@dataclass
class Project:
    root: str
    engines: dict
    services: dict
    axes: dict
    suites: dict
    storages: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    _rules_by_dir: dict = field(default_factory=dict)
    _used: set = field(default_factory=set)
    unused_rules: list = field(default_factory=list)
    default_storage: DefaultStorage | None = None  # None: `<repo>/data` and the default temp root
    pythonpath: tuple = ()  # absolute, in the order PYTHONPATH gave them
    inherit_env: tuple = ()  # names engines keep from the shell's env under an SDK prefix
    noted_requires_env: bool = False

    def __post_init__(self):
        if self.default_storage is None:
            self.default_storage = DefaultStorage(os.path.join(repo_root(self.root), "data"))

    def settle(self, test_path, body_file=None):
        """Apply every rule on the way from the root down to `test_path` (absolute); `body_file`,
        the test's `.test` file, is what a `requires_env` rule reads."""
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
                if rule.matches(rel, body_file):
                    self._used.add((conf_dir, n))
                    where = os.path.relpath(os.path.join(conf_dir, CONF_FILE), self.root)
                    settled.apply(rule.options, f"{where}: {rule.label}")
        return settled

    def note_unused_rules(self):
        for conf_dir, rules in self._rules_by_dir.items():
            for n, rule in enumerate(rules):
                if (conf_dir, n) not in self._used and rule.label != "default":
                    where = os.path.relpath(os.path.join(conf_dir, CONF_FILE), self.root)
                    self.unused_rules.append(f"{where}: {rule.label} matched no test")


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
    project = Project(root, {}, {}, {}, {})
    inherit_env(())
    root_file = os.path.join(root, CONF_FILE)
    if os.path.isfile(root_file):
        project.pythonpath = _literal_pythonpath(root_file, root)
        _apply_pythonpath(project.pythonpath)
    for dirpath, dirnames, filenames in os.walk(root):  # top-down, so the root file comes first
        for name in filenames:
            if _misnamed_conf(name):
                _log.warning("%s: not read; the file ducktest5 reads is %s", os.path.join(dirpath, name), CONF_FILE)
        if CONF_FILE not in filenames:
            _prune(dirnames)
            continue
        path = os.path.join(dirpath, CONF_FILE)
        module = _load(
            path, "ducktest_conf_" + os.path.relpath(dirpath, root).replace(os.sep, "_").replace(".", "root")
        )
        rules = list(getattr(module, "RULES", ()))
        if not all(isinstance(r, Rule) for r in rules):
            raise UsageError(f"{path}: RULES must hold default(...) and match(...) rules")
        if any(r.requires_env for r in rules) and not project.noted_requires_env:
            project.noted_requires_env = True
            _log.info(
                "%s: match(requires_env=...) is deprecated: a starting point for a repo that gates files "
                "with require-env; move to tags or paths",
                path,
            )
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
                "PYTHONPATH",
                "INHERIT_ENV",
            ):
                if hasattr(module, name):
                    raise UsageError(f"{path}: {name} belongs in the root {CONF_FILE} only; this file may hold RULES")
        _prune(dirnames)
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


def _prune(dirnames):
    dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")))


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
            unknown = [x for x in v.options.given.get("engines", ()) if x not in project.engines]
            if unknown:
                raise UsageError(f"{path}: AXES[{axis!r}] value {v.id!r} names engines {unknown} that are not declared")
            for kind in BINDINGS:
                if kind in v.options.given:
                    bound_def(project, kind, v.options.given[kind], f"{path}: AXES[{axis!r}] value {v.id!r}")


def bound_def(project, kind, ref, where):
    """The Def that `storage=`, `credential=` or `service=` names, by id."""
    declared, name = {
        "storage": (project.storages, "STORAGES"),
        "credential": (project.credentials, "CREDENTIALS"),
        "service": (project.services, "SERVICES"),
    }[kind]
    if ref not in declared:
        raise UsageError(f"{where}: {kind}={ref!r} is not in {name}")
    return declared[ref]
