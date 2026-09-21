"""scan: the test tree + the configuration files -> every case, before any selection.

No file is opened except `.py` test files (imported, to find `@test` bodies) and the configuration
files. A `.test` file is found by name only, so a tree of thousands of them costs one directory walk.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import sys

from . import asserts
from .binary import UsageError
from .conf import load_project
from .declare import CONF_FILE
from .model import BODY_SUFFIXES, PAIRED, PYTHON, SQLLOGIC, Case, TestDef

_NOT_TESTS = (CONF_FILE, "conftest.py", "__init__.py")


def _load(path, name):
    """Import one test file. Its own directory is importable while it loads, so that a test can
    import a helper beside it; a failure says which file it was."""
    spec = importlib.util.spec_from_file_location(name, path, loader=asserts.loader(name, path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(module)
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
    """Returns (cases, project): every case under the project root, in a stable order."""
    project = load_project(start)
    asserts.rewrite_under(project.root)
    bodies, scripts = _walk(project.root, project.excludes)
    body_by_stem = {}
    for b in bodies:
        body_by_stem.setdefault(os.path.splitext(b)[0], b)  # .test wins over .test_slow of one stem

    tests = []
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
            tests.append(
                TestDef(
                    path=rel,
                    name=name,
                    type=PAIRED if body_file else PYTHON,
                    root_path=_root_path(path, project.root),
                    body=decl.body,
                    body_file=os.path.relpath(body_file, working_dir) if body_file else None,
                    cases=dict(decl.cases),
                    tags=list(decl.tags) + (_suffix_tags(body_file) if body_file else []),
                    services=list(decl.services),
                    engines=list(decl.engines),
                    variants=list(decl.variants),
                )
            )
    for path in bodies:
        if path in paired_bodies:
            continue
        rel = os.path.relpath(path, working_dir)
        tests.append(
            TestDef(
                path=rel,
                name=os.path.splitext(os.path.basename(path))[0],
                type=SQLLOGIC,
                root_path=_root_path(path, project.root),
                body_file=rel,
                tags=_suffix_tags(path),
            )
        )

    cases = []
    for test in sorted(tests, key=lambda t: (t.root_path, t.name)):
        _settle(test, project, working_dir)
        for axis in test.variants:
            if axis not in project.axes:
                raise UsageError(
                    f"{test.path}::{test.name} takes part in axis {axis!r}; the project has {sorted(project.axes)}"
                )
        # AXES decides the order of the axis parts in an id, not the order in which rules name them.
        axes = [[(axis, v) for v in values.values()] for axis, values in project.axes.items() if axis in test.variants]
        for case_id in test.cases or [None]:
            for combo in itertools.product(*axes):
                cases.append(Case(test, case_id, dict(combo)))
    project.note_unused_rules()
    if project.unused_rules:
        raise UsageError(
            "a rule that matches no test is a typo or a moved file:\n  " + "\n  ".join(project.unused_rules)
        )
    for case in cases:
        for kind, known in (("services", project.services), ("engines", project.engines)):
            unknown = [x for x in getattr(case, kind) if x not in known]
            if unknown:
                raise UsageError(f"{case.id} needs {kind} {unknown}, not declared in the root {CONF_FILE}")
    return cases, project


def _settle(test, project, working_dir):
    """Rules first, from the root down; then the test's own declaration on top."""
    settled = project.settle(os.path.abspath(os.path.join(working_dir, test.path)))
    own = {"tags": test.tags, "services": test.services, "engines": test.engines, "variants": test.variants}
    for name in ("tags", "services", "engines", "variants"):
        merged = getattr(settled, name) + [x for x in own[name] if x not in getattr(settled, name)]
        setattr(test, name, merged)
    test.values, test.settings = settled.values, settled.settings
    test.in_default_set, test.timeout, test.retries = settled.in_default_set, settled.timeout, settled.retries
    test.why = settled.why
