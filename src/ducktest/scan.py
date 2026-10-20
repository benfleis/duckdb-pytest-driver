"""scan: the test tree + the configuration files -> every test, before any selection.

No file is opened except `.py` test files (imported, to find `@test` bodies) and the configuration
files. A `.test` file is found by name only, so a tree of thousands of them costs one directory walk.
"""

from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import sys
from dataclasses import fields

from . import asserts, leases
from . import names as _names
from .binary import UsageError
from .conf import bound_def, load_project
from .declare import CONF_FILE
from .outcomes import Skip
from .options import Settled
from .model import BODY_SUFFIXES, PAIRED, PYTHON, SQLLOGIC, Test, TestDef, Value
from .resources.dialect import rendered_classes, secret_renderer
from .resources import RO, RW, BootedEngineDef, CredentialDef, DefaultStorageDef, StorageDef, pick_credentials
from .resources.credential import AmbiguousCredential
from .resources.storage import At
from .testenv import resolve

_log = logging.getLogger(__name__)

_NOT_TESTS = (CONF_FILE, "conftest.py", "__init__.py")


def _bindings(test, project):
    """(slot, Def, source, ref) for each input a test binds, after its options merged (`options.py`)."""
    o = test.options
    return [(slot, bound_def(project, slot, ref), o.source_of("bind", slot), ref) for slot, ref in o.bind.items()]


def _roles(bindings, where):
    """storage, data, temp -> (StorageDef, `.at()` path or None); the default storage binds none."""
    for slot, d, source, ref in bindings:
        if isinstance(d, DefaultStorageDef) and isinstance(ref, At):
            raise UsageError(
                f"{where}: {slot}={ref!r} <- {source}: the default storage has no places; "
                "--data-dir and --temp-dir-root move it"
            )
    return {
        slot: (d, ref.path if isinstance(ref, At) else None)
        for slot, d, _, ref in bindings
        if isinstance(d, StorageDef) and not isinstance(d, DefaultStorageDef)
    }


def _ambiguous(exc, bindings, project, where):
    names = project.names
    sources = " and ".join(source for _, d, source, _ in bindings if d.name == exc.storage)
    candidates = []
    for c in sorted(exc.candidates, key=lambda c: c.name):
        found = names.declared_at(c.name, "credential")
        how = [names.show(found[0].where)] if found else []
        if c.name in names.pulled:
            how.append("pulled in by its scopes=")
        candidates.append(f"{c.name!r} ({', '.join(how)})" if how else repr(c.name))
    need = "read" if exc.mode == RO else "write to"
    return (
        f"{where}: storage {exc.storage!r} <- {sources}: credentials {', '.join(candidates)} are scoped "
        f"{exc.mode} to it and the test needs to {need} it; name one with credential= on the rule, "
        "or set default=True on one"
    )


def _inputs(bindings, project, where):
    """What a test binds, each once, and a credential for each storage by scope unless one is named. A
    storage it only reads fixtures from (`data=`) is a read-only edge."""
    bound = []
    for slot, d, _, _ in bindings:
        if not isinstance(d, DefaultStorageDef) and all(d is not b for b, _ in bound):
            bound.append((d, set()))
        if isinstance(d, StorageDef) and not isinstance(d, DefaultStorageDef):
            next(slots for b, slots in bound if b is d).add(slot)
    mode = {d.name: RO if slots == {"data"} else RW for d, slots in bound if isinstance(d, StorageDef)}
    named = [d for d, _ in bound if isinstance(d, CredentialDef)]
    try:
        credentials = pick_credentials(mode, project.credentials.values(), named)
    except AmbiguousCredential as exc:
        raise UsageError(_ambiguous(exc, bindings, project, where)) from None
    return tuple((d, mode.get(d.name, RW)) for d, _ in bound if not isinstance(d, CredentialDef)) + tuple(
        (c, RO) for c in credentials
    )


def _load(path, name):
    """Import one test file. Its own directory is importable while it loads, so that a test can
    import a helper beside it; a failure says which file it was."""
    spec = importlib.util.spec_from_file_location(name, path, loader=asserts.loader(name, path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(module)
    except UsageError:
        raise
    except Skip as exc:
        raise UsageError(
            f"{path} called skip({exc.reason!r}) while it was imported; skip inside the body, "
            "or leave the value out of matrix="
        ) from None
    except Exception as exc:
        raise UsageError(f"{path} could not be imported: {type(exc).__name__}: {exc}") from exc
    finally:
        sys.path.remove(os.path.dirname(path))
    return module


def _has_test_bodies(path):
    """Only a file that marks a body with `@test` is imported; a helper module beside the tests is not."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return "@test" in f.read()


def _walk(root):
    """(bodies, scripts): `.test`-like files and `.py` files under root, as absolute paths."""
    bodies, scripts = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")))
        for f in sorted(filenames):
            path = os.path.join(dirpath, f)
            if f.endswith(BODY_SUFFIXES):
                bodies.append(path)
            elif f.endswith(".py") and f not in _NOT_TESTS and _has_test_bodies(path):
                scripts.append(path)
    return bodies, scripts


def _root_path(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _suffix_tags(path):
    return ["slow"] if path.endswith(".test_slow") else ["coverage"] if path.endswith(".test_coverage") else []


def scan(start, *, working_dir=".", strict=False):
    """Returns (tests, project): every test under the project root, in a stable order. `strict`: a
    warning of the names check (a declaration nothing reaches, ...) is an error."""
    project = load_project(start)
    asserts.rewrite_under(project.root)
    bodies, scripts = _walk(project.root)
    body_by_stem = {}
    for b in bodies:
        body_by_stem.setdefault(os.path.splitext(b)[0], b)  # .test wins over .test_slow of one stem

    definitions = []  # (TestDef, what its @test(...) gives)
    paired_bodies = set()
    for path in scripts:
        rel = os.path.relpath(path, working_dir)
        # What a body's @test(...) names is reached too; a Def made while it imports is refused.
        project.names.body = os.path.abspath(path)
        try:
            with _names.loading(project.names):
                module = _load(path, "ducktest_tests." + rel.replace(os.sep, ".")[:-3])
        finally:
            project.names.body = None
        body_file = body_by_stem.get(os.path.splitext(path)[0])
        marked = [
            (name, obj.__ducktest__)
            for name, obj in vars(module).items()
            if getattr(obj, "__ducktest__", None) is not None and getattr(obj, "__module__", None) == module.__name__
        ]
        if body_file and not marked:
            raise UsageError(f"{rel} has the stem of {os.path.basename(body_file)} but no @test body")
        if body_file:
            paired_bodies.add(body_file)
        for name, decl in marked:
            project.body_options.append((f"{rel}::{name}: @test", decl.options))
            definitions.append(
                (
                    TestDef(
                        path=rel,
                        name=name,
                        type=PAIRED if body_file else PYTHON,
                        root_path=_root_path(path, project.root),
                        body=decl.body,
                        body_file=os.path.relpath(body_file, working_dir) if body_file else None,
                        tags=_suffix_tags(body_file) if body_file else [],
                    ),
                    decl.options,
                )
            )
    for path in bodies:
        if path in paired_bodies:
            continue
        rel = os.path.relpath(path, working_dir)
        definition = TestDef(
            path=rel,
            name=os.path.splitext(os.path.basename(path))[0],
            type=SQLLOGIC,
            root_path=_root_path(path, project.root),
            body_file=rel,
            tags=_suffix_tags(path),
        )
        definitions.append((definition, None))

    definitions.sort(key=lambda d: (d[0].root_path, d[0].name))
    for definition, own in definitions:
        _settle(definition, own, project, working_dir)
    project.declare(run_over={p for d, _ in definitions for p in d.matrix if isinstance(p, str)}, strict=strict)

    tests = []
    for definition, _ in definitions:
        _engines(definition, project)
        for cell in itertools.product(*_parameters(definition, project)):
            test = Test(definition, {v.parameter: v for v in cell})
            bindings = _bindings(test, project)
            test.bind = tuple(
                (slot, str(ref) if isinstance(ref, At) else d.name, source) for slot, d, source, ref in bindings
            )
            test.roles = _roles(bindings, test.name)
            test.inputs = _inputs(bindings, project, test.name)
            tests.append(test)
    project.note_unused_rules()
    if project.unused_rules:
        raise UsageError(
            "a rule that matches no test is a typo or a moved file:\n  " + "\n  ".join(project.unused_rules)
        )
    for test in tests:
        unknown = [x for x in test.engines if x not in project.engines]
        if unknown:
            raise UsageError(f"{test.name} needs engines {unknown}, not declared in Project(engines=...)")
        # A test's DuckDB engine may come from the project, not the test: settle it before the run.
        leases.check(test.definition, [leases.duckdb_of(test, project)])
        _one_set_per_type(test)
        _renderable(test, project)
        _templates_held(test, project)
    _one_set_per_booted_engine(tests, project)
    _warn_ungiven_env_gates(tests, project, working_dir)
    return tests, project


def _warn_ungiven_env_gates(tests, project, working_dir):
    """A `requires_env` rule only selects: unless something gives the name, the binary skips the file."""
    for test in tests:
        gates = project.env_gates.get(os.path.abspath(os.path.join(working_dir, test.definition.path)), [])
        if not gates:
            continue
        # `require-env` reads the binary's process env: a value, a `process_env=` entry or a credential's env.
        env = resolve(test)
        given = {name.lower() for name in env.values}
        given_env = {n for n, state in env.process_state.items() if state == "set"} | set(env.pending_env)
        for where, name in gates:
            if name.lower() not in given and name not in given_env:
                _log.warning(
                    "%s: match(requires_env=%r) selects %s but nothing gives %s; add values={%r: '1'} or the test skips",
                    where,
                    name,
                    test.name,
                    name,
                    name,
                )


def _parameters(definition, project):
    """The values of each parameter a definition runs over, in the order its `matrix` names them:
    the rules' from the root down, then `@test`'s. That order is the order of the parts of an id."""
    out = []
    for item in definition.matrix:
        if isinstance(item, str):
            if item not in project.matrix:
                raise UsageError(
                    f"{definition.path}::{definition.name} runs over parameter {item!r}; the project's matrix "
                    f"has {sorted(project.matrix)}"
                )
            out.append([Value(item, name, options) for name, options in project.matrix[item].items()])
            continue
        if item.name in project.matrix:
            raise UsageError(
                f"{definition.path}::{definition.name}: {definition.source_of('matrix', item.name)} defines "
                f"parameter {item.name!r}, which the project's matrix has; give it another name"
            )
        out.append([Value(item.name, name, data=data) for name, data in item.values.items()])
    return out


_FIX = {
    "service": "bind it (service={0}) or bind a storage or credential of it",
    "storage": "bind it (storage={0}, data= or temp=)",
    "credential": "bind it (credential={0})",
}


def _templates_held(test, project):
    """Each `{name.attr}` in a test's values, settings and init_sql names an input the test holds, or an
    input of one: the run fills nothing else, so a template that names anything else would stay as written."""
    registry = {**project.services, **project.storages, **project.credentials}
    held = set()

    def walk(d):
        if d is not None and d.name not in held:
            held.add(d.name)
            for name, _, sub in d.edges():
                walk(sub if sub is not None else registry.get(name))

    for d, _ in test.inputs:
        walk(d)
    o = test.options
    texts = [("values", k, v) for k, v in o.values.items()] + [("settings", k, v) for k, v in o.settings.items()]
    texts += [("init_sql", n, sql) for n, sql in enumerate(o.init_sql)]
    for field_name, key, text in texts:
        for m in _names.ATTRIBUTE_REF.finditer(text) if isinstance(text, str) else ():
            name = m.group(1)
            if name in held:
                continue
            kind = _names.kind_of(registry.get(name) or project.engines.get(name)) or "resource"
            fix = _FIX[kind].format(name) if kind in _FIX else "a template names a service, storage or credential"
            shown = f"{field_name} {key}" if field_name != "init_sql" else f"init_sql[{key}]"
            raise UsageError(
                f"{test.name}: {o.source_of(field_name, key)}: {shown} names '{m.group()}' but the test does not "
                f"hold {kind} {name!r}; {fix}"
            )


def _renderable(test, project):
    """Each engine of the test renders the secret of each credential that has one."""
    storages = {d.name for d, _ in test.inputs if isinstance(d, StorageDef)}
    engines = [] if test.definition.type == SQLLOGIC else leases.engines_of(test, project)
    dialects = [(d.name, d.dialect) for d in engines if d.dialect is not None]
    if test.definition.body_file:
        dialects.append(("unittest", "unittest"))
    for c, _ in test.inputs:
        if not isinstance(c, CredentialDef) or not storages & set(c.scope_storages):
            continue
        for engine_id, dialect in dialects:
            if secret_renderer(dialect, type(c)) is None:
                raise UsageError(
                    f"{test.name}: engine {engine_id!r} cannot render {type(c).__name__} {c.name!r} "
                    f"({dialect} renders: {', '.join(rendered_classes(dialect)) or 'none'})"
                )


def _one_set_per_booted_engine(tests, project):
    """A booted engine gets the credentials of its tests when it boots, once per run: one of each type."""
    for engine_id, d in project.engines.items():
        if not isinstance(d, BootedEngineDef):
            continue
        by_type = {}
        for test in tests:
            if engine_id in test.engines:
                for c, _ in test.inputs:
                    if isinstance(c, CredentialDef) and c.name not in by_type.setdefault(type(c), []):
                        by_type[type(c)].append(c.name)
        for cls, ids in by_type.items():
            if len(ids) > 1:
                raise UsageError(
                    f"engine {engine_id!r}: its tests hold {cls.__name__} {' and '.join(repr(i) for i in ids)} "
                    "by env, and it boots once per run: declare a second engine of its kind for the other tests"
                )


def _one_set_per_type(test):
    """A process has one set of each SDK's env names, so a test holds one credential of a type."""
    by_type = {}
    for d, _ in test.inputs:
        if isinstance(d, CredentialDef):
            by_type.setdefault(type(d), []).append(d.name)
    for cls, ids in by_type.items():
        if len(ids) > 1:
            names = " and ".join(repr(i) for i in ids)
            raise UsageError(
                f"{test.name}: holds {cls.__name__} {names} by {'/'.join(p + '*' for p in cls.sdk_prefixes)} env; "
                "one process has one set. Several credentials of one type in one test are not supported yet; "
                "split the test"
            )


def _engines(definition, project):
    """The engines a definition names exist."""
    for name in definition.engines:
        if name not in project.engines:
            raise UsageError(
                f"{definition.path}::{definition.name} names engine {name!r}; the project's engines are {sorted(project.engines)}"
            )
    leases.check(definition, [project.engines[name] for name in definition.engines])


def _settle(definition, own, project, working_dir):
    """Rules first, from the root down; then the definition's own `@test(...)` on top."""
    body_file = os.path.abspath(os.path.join(working_dir, definition.body_file)) if definition.body_file else None
    settled = project.settle(os.path.abspath(os.path.join(working_dir, definition.path)), body_file)
    if own is not None:
        settled.apply(own, "@test")
    suffix = [t for t in definition.tags if t not in settled.tags]
    for f in fields(Settled):
        setattr(definition, f.name, getattr(settled, f.name))
    definition.tags += suffix
