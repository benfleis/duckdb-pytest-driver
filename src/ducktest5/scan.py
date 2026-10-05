"""scan: the test tree + the configuration files -> every test, before any selection.

No file is opened except `.py` test files (imported, to find `@test` bodies) and the configuration
files. A `.test` file is found by name only, so a tree of thousands of them costs one directory walk.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import sys
from dataclasses import fields

from . import asserts, leases
from .binary import UsageError
from .conf import bound_def, load_project
from .declare import CONF_FILE
from .outcomes import Skip
from .options import Settled
from .model import BODY_SUFFIXES, PAIRED, PYTHON, SQLLOGIC, Test, TestDef
from .resources.dialect import rendered_classes, secret_renderer
from .resources import RO, RW, BootedEngineDef, CredentialDef, DefaultStorageDef, StorageDef, pick_credentials

_NOT_TESTS = (CONF_FILE, "conftest.py", "__init__.py")


def _bindings(test, project):
    """(kind, Def, source) for each input a test binds, after its options merged (`options.py`)."""
    o = test.options
    return [(kind, bound_def(project, kind, ref, test.id), o.source_of("bind", kind)) for kind, ref in o.bind.items()]


def _inputs(bindings, project):
    """What a test binds, and a credential for each storage by scope unless one is named."""
    bound = [d for _, d, _ in bindings if not isinstance(d, DefaultStorageDef)]  # the default storage is none
    storages = {d.id: RW for d in bound if isinstance(d, StorageDef)}
    named = [d for d in bound if isinstance(d, CredentialDef)]
    credentials = pick_credentials(storages, project.credentials.values(), named)
    return tuple((d, RW) for d in bound if not isinstance(d, CredentialDef)) + tuple((c, RO) for c in credentials)


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
            "or leave the case out of params="
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


def _walk(root, excludes):
    """(bodies, scripts): `.test`-like files and `.py` files under root, as absolute paths."""
    bodies, scripts = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")))
        for f in sorted(filenames):
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if excludes(rel):
                continue
            if f.endswith(BODY_SUFFIXES):
                bodies.append(path)
            elif f.endswith(".py") and f not in _NOT_TESTS and _has_test_bodies(path):
                scripts.append(path)
    return bodies, scripts


def _root_path(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _suffix_tags(path):
    return ["slow"] if path.endswith(".test_slow") else ["coverage"] if path.endswith(".test_coverage") else []


def scan(start, *, working_dir="."):
    """Returns (tests, project): every test under the project root, in a stable order."""
    project = load_project(start)
    asserts.rewrite_under(project.root)
    bodies, scripts = _walk(project.root, project.excludes)
    body_by_stem = {}
    for b in bodies:
        body_by_stem.setdefault(os.path.splitext(b)[0], b)  # .test wins over .test_slow of one stem

    definitions = []  # (TestDef, what its @test(...) gives)
    paired_bodies = set()
    for path in scripts:
        rel = os.path.relpath(path, working_dir)
        module = _load(path, "ducktest_tests." + rel.replace(os.sep, ".")[:-3])
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
            definitions.append(
                (
                    TestDef(
                        path=rel,
                        name=name,
                        type=PAIRED if body_file else PYTHON,
                        root_path=_root_path(path, project.root),
                        body=decl.body,
                        body_file=os.path.relpath(body_file, working_dir) if body_file else None,
                        params=dict(decl.params),
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

    tests = []
    for definition, own in sorted(definitions, key=lambda d: (d[0].root_path, d[0].name)):
        _settle(definition, own, project, working_dir)
        _engines(definition, project)
        for axis in definition.axes:
            if axis not in project.axes:
                raise UsageError(
                    f"{definition.path}::{definition.name} takes part in axis {axis!r}; the project has {sorted(project.axes)}"
                )
        # AXES decides the order of the axis parts in an id, not the order in which rules name them.
        axes = [
            [(axis, v) for v in values.values()] for axis, values in project.axes.items() if axis in definition.axes
        ]
        for params_key in definition.params or [None]:
            for combo in itertools.product(*axes):
                test = Test(definition, params_key, dict(combo))
                bindings = _bindings(test, project)
                test.bind = tuple((kind, d.id, source) for kind, d, source in bindings)
                test.inputs = _inputs(bindings, project)
                tests.append(test)
    project.note_unused_rules()
    if project.unused_rules:
        raise UsageError(
            "a rule that matches no test is a typo or a moved file:\n  " + "\n  ".join(project.unused_rules)
        )
    for test in tests:
        unknown = [x for x in test.engines if x not in project.engines]
        if unknown:
            raise UsageError(f"{test.id} needs engines {unknown}, not declared in the root {CONF_FILE}")
        # A test's DuckDB engine may come from the project, not the test: settle it before the run.
        leases.check(test.definition, [leases.duckdb_of(test, project)])
        _one_set_per_type(test)
        _renderable(test, project)
    _one_set_per_booted_engine(tests, project)
    return tests, project


def _renderable(test, project):
    """Each engine of the test renders the secret of each credential that has one."""
    storages = {d.id for d, _ in test.inputs if isinstance(d, StorageDef)}
    engines = [] if test.definition.type == SQLLOGIC else leases.engines_of(test, project)
    dialects = [(d.id, d.dialect) for d in engines if d.dialect is not None]
    if test.definition.body_file:
        dialects.append(("unittest", "unittest"))
    for c, _ in test.inputs:
        if not isinstance(c, CredentialDef) or not storages & set(c.scope_storages):
            continue
        for engine_id, dialect in dialects:
            if secret_renderer(dialect, type(c)) is None:
                raise UsageError(
                    f"{test.id}: engine {engine_id!r} cannot render {type(c).__name__} {c.id!r} "
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
                    if isinstance(c, CredentialDef) and c.id not in by_type.setdefault(type(c), []):
                        by_type[type(c)].append(c.id)
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
            by_type.setdefault(type(d), []).append(d.id)
    for cls, ids in by_type.items():
        if len(ids) > 1:
            names = " and ".join(repr(i) for i in ids)
            raise UsageError(
                f"{test.id}: holds {cls.__name__} {names} by {'/'.join(p + '*' for p in cls.sdk_prefixes)} env; "
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
