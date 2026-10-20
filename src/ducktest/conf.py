"""`ducktest_conf.py`: configuration for a directory and everything below it.

The file at the root of the scanned tree is the project file. It holds one `Project(...)`, in a
variable of any name; a file below it holds one `Rules(...)`. The loader finds them by a scan of the
module's top-level names: two of them in one file is an error, and so is a `Project` below the root
or a `Rules` in it; a file with neither is a warning.

    project = Project(
        engines=[DuckDBEngineDef("duck", requires=["delta"])],
        matrix={
            "storage": {
                "local": {"storage": LOCAL_DIR, "temp": LOCAL_DIR},
                "s3": {"storage": LOCAL_LAKE, "temp": LOCAL_LAKE},
            }
        },
        rules=[match("cross_engine/**", matrix=["storage"])],
    )

The project's services, storages and credentials are what the rules, the matrix values and the bodies'
`@test(...)` name, with what those pull: a storage brings its service and the credential scoped to it, a
credential the service it is made of (`names.py`). `Project(services=, storages=, credentials=)` adds to
that set and is never needed; the checks run on the set once the bodies are imported. `engines=` is
different: an engine is not reached through what a test binds, and the project's one DuckDB engine is the
default of every test only when it is listed there.

`inherit_env=["AWS_CA_BUNDLE"]` names variables engine processes keep from the shell's env. Every
other name under an SDK prefix (`AWS_`) is left out of them: an SDK reads it with getenv(), so a test
that gets it from the shell can pass with one person's setup and fail elsewhere. Keys come from a
credential, never from here.

Helper code goes on `sys.path` from two places. The base is `[tool.ducktest] pythonpath = [...]` in
the repo's `pyproject.toml`, relative to it, read before any conf runs, so a conf's own imports may
use it. `Project(pythonpath=[...])` adds more, relative to the project file, from that point on. Both
hold for the whole process, so every body sees them; engine processes do not get them.

Any directory may have its own file with rules. A rule
speaks about the whole subtree (`default`) or about a pattern relative to its file (`match`); a
pattern may name one file.

A rule takes the options `options.py` lists (the slots), as `@test(...)` and a matrix value do, and
they merge in one order: the root file first, then each file on the way down to the test definition;
in a file, the rules as listed; then the definition's own `@test(...)`, then the matrix values of
its cell. `why` records which place gave what.

`matrix={"storage": {"local": "default", "s3": LOCAL_LAKE}}` declares the project's parameters: each
maps a value name to a dict of slots, or to a name or a declaration that fills the slot the parameter
is named after (`storage`, `data`, `temp`, `credential`, `service`). `matrix=["storage"]` on a rule or
`@test` runs a test once per value; `matrix=[{"case": CASES}]` defines a parameter of that place's own.
A cell is one more rule, applied last, that binds the slots its value names; a test's roots follow
from whatever that binds, as from any rule.

`engines=` holds `EngineDef`s (`DuckDBEngineDef(...)`, `SparkEngineDef.delta(...)`, ...); `engines=["duck"]` on a
rule names the engines a test drives, each through a Client of its own. An engine stays a kind of service
internally -- one lifecycle, one provisioning path -- so a service that is an input of an engine is one of
the project's services too.

`mutex="name"`, or several names, says what a test must have to itself: no two tests that name one of
them run at the same time, however many workers the run has.

A test is in a suite by what it carries: its path, its tags, the values of its cell. `suites=` names
selections over those (`select.py`); `tags=["opt-in"]` keeps a test out of a bare run.

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
import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import names as _names
from .binary import UsageError
from .declare import CONF_FILE
from .options import BINDINGS, Options, Settled, check_parameter, kind_of_slot
from .resources import CredentialDef, DefaultStorage, EngineDef, ServiceDef, StorageDef
from .resources.storage import At, declared_scheme
from .resources.credential import inherit_env

_log = logging.getLogger(__name__)


def default(**fields) -> Rule:
    """Convenience for `Rule.default(...)`."""
    return Rule.default(**fields)


def match(pattern: str, *, requires_env: str | None = None, **fields) -> Rule:
    """Convenience for `Rule.match(...)`."""
    return Rule.match(pattern, requires_env=requires_env, **fields)


@dataclass(frozen=True)
class Rule:
    """What a conf file gives the tests below it, all of them or by path; made by `Rule.default(...)` or
    `Rule.match(...)`."""

    pattern: str
    options: Options
    requires_env: str | None = None

    def matches(self, rel_to_conf, body_file=None, twin_rel=None):
        glob = _glob(self.pattern)
        if glob.fullmatch(rel_to_conf) is None and (twin_rel is None or glob.fullmatch(twin_rel) is None):
            return False
        return self.requires_env is None or (body_file is not None and self.requires_env in _env_gates(body_file))

    @property
    def label(self):
        if self.pattern == "**" and self.requires_env is None:
            return "default"
        return f"match {self.pattern!r}" + (f", requires_env={self.requires_env!r}" if self.requires_env else "")

    @classmethod
    def default(cls, **fields) -> Rule:
        """A rule for every test definition below this file."""
        return cls("**", Options.of(fields, "rule '**'"))

    @classmethod
    def match(cls, pattern: str, *, requires_env: str | None = None, **fields) -> Rule:
        """A rule for the test definitions whose path, relative to this file, matches `pattern`: `*` stays
        inside one directory level, `**` crosses levels, `dir/**` is everything below `dir`, and a pattern
        with no wildcard names one file. A body paired with a `.test` matches by either file's path.

        `requires_env="NAME"` narrows it to the `.test` files that hold a `require-env NAME` line, with
        or without a value. Deprecated: a starting point for a repo that gates files with `require-env`;
        move to tags or paths."""
        if requires_env is not None and not isinstance(requires_env, str):
            raise UsageError(f"rule {pattern!r}: requires_env={requires_env!r}; it is one name")
        return cls(pattern, Options.of(fields, f"rule {pattern!r}"), requires_env)


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


_LISTS = ("engines", "services", "storages", "credentials", "inherit_env", "pythonpath")


@dataclass(kw_only=True)
class Project:
    """What the root `ducktest_conf.py` declares, as `project = Project(...)`. Every field is optional.

    engines       DuckDB and Spark engines the tests drive: `DuckDBEngineDef(...)`, `SparkEngineDef.delta(...)`
    services      more servers, besides those the rules and tests reach: `S3ServiceDef.rustfs(...)`,
                  `AzureServiceDef.azurite(...)`, `HttpServerDef(...)`
    storages      more storages, besides those the rules and tests reach: `LOCAL_S3.storage(...)`
    credentials   more keys, besides those the rules and tests reach: `LOCAL_S3.credential(...)`
    matrix        parameter -> value name -> a dict of slots, or a name or declaration that fills the
                  slot named like the parameter: {"storage": {"local": "default", "s3": LOCAL_LAKE}}
    suites        named selections over tags and paths (`all` is built in)
    inherit_env   names engine processes keep from the shell's env despite the SDK-prefix removal
    rules         `default(...)` and `match(...)`, in order
    pythonpath    helper directories, relative to this file, on `sys.path` from this point on

    A reference slot (`storage=`, `data=`, `temp=`, `credential=`, `service=`, `engines=[...]`,
    `scopes=[...]`, a matrix value) takes a name or the declaration itself; `data=` and `temp=` also
    take `STORAGE.at("sub/path")`, a place under a storage."""

    engines: Sequence[EngineDef] = ()
    services: Sequence[ServiceDef] = ()
    storages: Sequence[StorageDef] = ()
    credentials: Sequence[CredentialDef] = ()
    matrix: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    suites: Mapping[str, object] = field(default_factory=dict)
    inherit_env: Sequence[str] = ()
    rules: Sequence[Rule] = ()
    pythonpath: Sequence[str] = ()

    def __post_init__(self):
        names = _names.current()
        conf = names.conf if names is not None else None
        where = f"{conf}: Project(...)" if conf else "Project(...)"
        for name in _LISTS:
            value = getattr(self, name)
            if isinstance(value, (str, Mapping)) or not isinstance(value, (list, tuple)):
                raise UsageError(f"{where}: {name}={value!r}; it is a list")
            setattr(self, name, tuple(value))
        self.rules = tuple(_rules(self.rules, f"{where}: rules="))
        self.matrix = _matrix(self.matrix, where)
        # An addition holds from here on: for the imports below this call and for every body.
        self.added_pythonpath = ()
        if conf is not None:
            self.added_pythonpath = _pythonpath(self.pythonpath, os.path.dirname(conf), f"{where}: pythonpath")
            _apply_pythonpath(self.added_pythonpath)


class Rules:
    """What a `ducktest_conf.py` below the root declares: `rules = Rules(default(...), match(...), ...)`,
    for the tests below that file, in order; the root file holds its rules in `Project(rules=...)`."""

    def __init__(self, *rules: Rule):
        self.rules = tuple(_rules(rules, "Rules(...)"))

    def __repr__(self):
        return f"Rules({', '.join(r.label for r in self.rules)})"


def _rules(rules, where):
    if len(rules) == 1 and isinstance(rules[0], (list, tuple)) and where == "Rules(...)":
        raise UsageError("Rules(...) takes the rules themselves, Rules(match(...), ...), not a list")
    if isinstance(rules, (str, Rule)) or not isinstance(rules, (list, tuple)):
        raise UsageError(f"{where} {rules!r}; it is a list of default(...) and match(...) rules")
    bad = next((r for r in rules if not isinstance(r, Rule)), None)
    if bad is not None:
        raise UsageError(f"{where} holds default(...) and match(...) rules; {bad!r} is not one")
    return rules


@dataclass
class LoadedProject:
    """The project as the loader read it: the root's `Project` and the rules of every file, by directory."""

    root: str
    engines: dict
    services: dict
    matrix: dict  # parameter -> value name -> Options
    suites: dict
    storages: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    _rules_by_dir: dict = field(default_factory=dict)
    _used: set = field(default_factory=set)
    unused_rules: list = field(default_factory=list)
    default_storage: DefaultStorage | None = None  # None: `<repo>/data` and the default temp root
    pythonpath: tuple = ()  # absolute: pyproject.toml's, then the Project's additions
    base_pythonpath: tuple = ()  # the part pyproject.toml gives
    inherit_env: tuple = ()  # names engines keep from the shell's env under an SDK prefix
    noted_requires_env: bool = False
    env_gates: dict = field(default_factory=dict)  # test path -> (conf file, name) of each requires_env rule on it
    names: _names.Names | None = None  # what the confs and the bodies declared and referred to
    listed: dict = field(default_factory=dict)  # kind -> what Project(...) lists of it, as written
    body_options: list = field(default_factory=list)  # (label, Options) of each body's @test(...)
    project_file: str = CONF_FILE  # the root conf file, relative to the root
    unused: list = field(default_factory=list)  # declarations nothing uses

    def __post_init__(self):
        if self.default_storage is None:
            self.default_storage = DefaultStorage(os.path.join(repo_root(self.root), "data"))

    def pythonpath_by_source(self) -> dict:
        added = self.pythonpath[len(self.base_pythonpath) :]
        out = {"pyproject.toml [tool.ducktest]": self.base_pythonpath, CONF_FILE: added}
        return {source: paths for source, paths in out.items() if paths}

    def settle(self, test_path, body_file=None):
        """Apply every rule on the way from the root down to `test_path` (absolute); `body_file`,
        the test's `.test` file, is what a `requires_env` rule reads, and a paired body matches by its path too."""
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
            twin_rel = os.path.relpath(body_file, conf_dir).replace(os.sep, "/") if body_file else None
            for n, rule in enumerate(self._rules_by_dir[conf_dir]):
                if rule.matches(rel, body_file, twin_rel):
                    self._used.add((conf_dir, n))
                    where = os.path.relpath(os.path.join(conf_dir, CONF_FILE), self.root)
                    settled.apply(rule.options, f"{where}: {rule.label}")
                    if rule.requires_env is not None:
                        self.env_gates.setdefault(test_path, []).append((where, rule.requires_env))
        return settled

    def declare(self, *, run_over=None, strict=False):
        """The services, storages and credentials of the project are what the rules, the matrix values
        and the bodies reach, with what those pull, and what `Project(...)` lists; check them. Call it once
        the bodies are imported: their `@test(...)` reaches too."""
        declared, self.unused = self.names.derive(self, run_over=run_over, strict=strict)
        self.services, self.storages, self.credentials = (declared[k] for k in ("service", "storage", "credential"))
        _check_declared(self)
        for warning in self.unused:
            _log.warning("%s", warning)

    def declare_by_name(self, services=(), storages=()):
        """For `cleanup`, which reads no body: the services and storages a run used, by name, as the conf
        files declare them now. No check runs; a name no conf declares as that kind is logged and left out."""
        made = {d.name: d.obj for d in self.names.live()}
        for kind, wanted, out in (("service", services, self.services), ("storage", storages, self.storages)):
            for name in wanted:
                d = made.get(name)
                if _names.kind_of(d) != kind:
                    _log.warning("%s %r: the run used it and no %s declares it now; not cleaned", kind, name, CONF_FILE)
                    continue
                out[name] = d

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


def _pythonpath(given, base, where):
    """`given` as absolute directories, each relative to `base` and inside its repository."""
    if (
        isinstance(given, str)
        or not isinstance(given, (list, tuple))
        or not all(isinstance(p, str) and p for p in given)
    ):
        shown = list(given) if isinstance(given, tuple) else given
        raise UsageError(f'{where} = {shown!r}; it is a list of paths, such as ["py", "../scripts"]')
    top, out = repo_root(base), []
    for p in given:
        full = os.path.normpath(os.path.join(base, p))
        if os.path.isabs(p) or os.path.commonpath([top, full]) != top:
            raise UsageError(f"{where}: {p!r} is outside the repository {top}; give a path relative to {base}")
        if not os.path.isdir(full):
            raise UsageError(f"{where}: {p!r} is not a directory ({full})")
        out.append(full)
    return tuple(out)


def base_pythonpath(repo):
    """`[tool.ducktest] pythonpath` of the repo's `pyproject.toml`, as absolute directories."""
    path = os.path.join(repo, "pyproject.toml")
    if not os.path.isfile(path):
        return ()
    try:
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path}: {exc}") from None
    given = doc.get("tool", {}).get("ducktest", {}).get("pythonpath", [])
    return _pythonpath(given, repo, f"{path}: [tool.ducktest] pythonpath")


def _apply_pythonpath(paths):
    for p in reversed(paths):
        if p not in sys.path:
            sys.path.insert(0, p)


def load_project(start):
    """Every `ducktest_conf.py` from the root down. Its services, storages and credentials are not known
    until `declare()`: the bodies reach some of them."""
    root = find_root(start)
    project = LoadedProject(root, {}, {}, {}, {})
    inherit_env(())
    project.pythonpath = project.base_pythonpath = base_pythonpath(repo_root(root))
    _apply_pythonpath(project.pythonpath)
    project.names = names = _names.Names(root=root)
    with _names.loading(names):
        for dirpath, dirnames, filenames in os.walk(root):  # top-down, so the root file comes first
            for name in filenames:
                if _misnamed_conf(name):
                    _log.warning("%s: not read; the file ducktest reads is %s", os.path.join(dirpath, name), CONF_FILE)
            if CONF_FILE not in filenames:
                _prune(dirnames)
                continue
            path = os.path.join(dirpath, CONF_FILE)
            names.conf = path
            names.confs.add(os.path.abspath(path))
            try:
                module = _load(
                    path, "ducktest_conf_" + os.path.relpath(dirpath, root).replace(os.sep, "_").replace(".", "root")
                )
            finally:
                names.conf = None
            declared = _declaration(module, path, is_root=dirpath == root)
            rules = list(declared.rules) if declared is not None else []
            if any(r.requires_env for r in rules) and not project.noted_requires_env:
                project.noted_requires_env = True
                _log.info(
                    "%s: match(requires_env=...) is deprecated: a starting point for a repo that gates files "
                    "with require-env; move to tags or paths",
                    path,
                )
            project._rules_by_dir[dirpath] = rules
            if isinstance(declared, Project):
                project.engines = {e.name: e for e in declared.engines}
                project.listed = {
                    "engine": declared.engines,
                    "service": declared.services,
                    "storage": declared.storages,
                    "credential": declared.credentials,
                }
                project.matrix = declared.matrix
                project.project_file = os.path.relpath(path, root)
                project.suites = dict(declared.suites)
                if "all" in project.suites:
                    raise UsageError(
                        f"{path}: a project may not define a suite named 'all' -- it always exists, built in"
                    )
                project.inherit_env = inherit_env(list(declared.inherit_env), f"{path}: inherit_env")
                project.pythonpath += declared.added_pythonpath
                _check_listed(path, declared)
            _prune(dirnames)
    return project


def _declaration(module, path, *, is_root):
    """The one `Project` or `Rules` among the module's top-level names, or None."""
    found, seen = [], set()
    for k, v in vars(module).items():
        if isinstance(v, (Project, Rules)) and id(v) not in seen:
            seen.add(id(v))
            found.append((k, v))
    if len(found) > 1:
        held = ", ".join(f"{k} = {type(v).__name__}(...)" for k, v in found)
        raise UsageError(
            f"{path}: holds {held}; a {CONF_FILE} holds one, Project(...) at the root, Rules(...) below it"
        )
    if not found:
        _log.warning("%s: holds no Project(...) or Rules(...); it gives the tests nothing", path)
        return None
    name, declared = found[0]
    if isinstance(declared, Project) and not is_root:
        raise UsageError(
            f"{path}: {name} = Project(...) belongs in the root {CONF_FILE}; a file below it holds its rules "
            "as rules = Rules(default(...), match(...), ...)"
        )
    if isinstance(declared, Rules) and is_root:
        raise UsageError(
            f"{path}: {name} = Rules(...) belongs in a file below the root; the root {CONF_FILE} holds its rules "
            "as project = Project(rules=[default(...), match(...), ...])"
        )
    return declared


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


def _check_listed(path, declared):
    """What `Project(...)` lists is of the kind each list holds."""
    for name, kind, given in (
        ("storages", StorageDef, declared.storages),
        ("credentials", CredentialDef, declared.credentials),
    ):
        bad = next((d for d in given if not isinstance(d, kind)), None)
        if bad is not None:
            raise UsageError(
                f"{path}: Project({name}=...) holds {kind.__name__} declarations; {_label(bad)} is not one"
            )
    bad_engine = next((e for e in declared.engines if not isinstance(e, EngineDef)), None)
    if bad_engine is not None:
        raise UsageError(f"{path}: {_label(bad_engine)} is not an engine; declare services in Project(services=...)")
    bad_service = next((s for s in declared.services if isinstance(s, EngineDef)), None)
    if bad_service is not None:
        raise UsageError(f"{path}: {_label(bad_service)} is an engine; declare engines in Project(engines=...)")
    bad = next((s for s in declared.services if not isinstance(s, ServiceDef)), None)
    if bad is not None:
        raise UsageError(f"{path}: Project(services=...) holds ServiceDef declarations; {_label(bad)} is not one")


def _check_declared(project):
    """Each storage of the declared set is one its service serves, and each credential is for the
    scheme of the storages it is scoped to."""
    path = os.path.join(project.root, project.project_file)
    for storage in project.storages.values():
        for _, _, service in storage.edges():
            if isinstance(service, ServiceDef):
                service.check_storage(storage)
    for c in project.credentials.values():
        for storage_name in c.scope_storages:
            storage = project.storages.get(storage_name)
            scheme = declared_scheme(storage.root) if storage is not None else None
            if scheme is not None and scheme not in type(c).schemes:
                raise UsageError(
                    f"{path}: {c.label} is scoped to storage {storage_name!r} ({storage.root}): an "
                    f"{type(c).__name__} is for {', '.join(type(c).schemes)} storages only"
                )


def _label(obj):
    name = getattr(obj, "name", None)
    return f"{type(obj).__name__}({name!r})" if isinstance(name, str) else repr(obj)


def _matrix(given, where):
    """parameter -> value name -> a dict of slots, or a name or declaration that fills the slot the
    parameter is named after."""
    if not isinstance(given, Mapping):
        raise UsageError(f"{where}: matrix= is a dict of parameter -> {{value name: value}}")
    out = {}
    for parameter, values in given.items():
        check_parameter(parameter, values, f"{where}: matrix")
        out[parameter] = {}
        for name, value in values.items():
            at = f"{where}: matrix[{parameter!r}][{name!r}]"
            if isinstance(value, (str, At)) or _names.kind_of(value) is not None:
                if parameter not in BINDINGS:
                    raise UsageError(
                        f"{at} = {value!r}: a name or a declaration fills the slot the parameter is named after, "
                        f"and {parameter!r} is no slot ({', '.join(BINDINGS)}); give a dict of slots"
                    )
                value = {parameter: value}
            elif not isinstance(value, dict):
                raise UsageError(f"{at} = {value!r}; a value is a dict of slots")
            out[parameter][name] = Options.of(value, at, leave_out=("matrix",))
    return out


def bound_def(project, slot, ref, where):
    """The Def that a binding slot (`storage=`, `temp=`, `credential=`, ...) names; for `At`, its storage's."""
    kind = kind_of_slot(slot)
    declared = {"storage": project.storages, "credential": project.credentials, "service": project.services}[kind]
    name = ref.name if isinstance(ref, At) else ref
    if name in declared:
        return declared[name]
    raise UsageError(f"{where}: {slot}={ref!r} is not declared; the project's are {sorted(declared)}")
