"""scan: test tree + declarations + --select -> one object-free record per test x case."""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass

from .binary import UsageError
from .declare import PROJECT_FILE, Engine, Test


@dataclass(frozen=True)
class Item:
    id: str
    test: Test
    case_id: str | None
    path: str

    @property
    def case(self):
        return self.test.cases.get(self.case_id, {})

    def record(self, engines):
        return {
            "identity": {
                "id": self.id,
                "source": {"path": self.path, "function": self.test.body.__name__},
                "case": self.case_id,
            },
            "selection": {"tags": list(self.test.tags)},
            "preconditions": {
                "engines": [{"id": e, "variant": engines[e].variant} for e in self.test.engines],
                "clients": ["duckdb-shell", *self.test.engines],
            },
            "runtime": {"case": self.case},
        }


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_project(root):
    """The engines declared in the nearest `ducktest_project.py` at or above `root`."""
    here = os.path.abspath(root if os.path.isdir(root) else os.path.dirname(root))
    while True:
        candidate = os.path.join(here, PROJECT_FILE)
        if os.path.isfile(candidate):
            project = _load(candidate, "ducktest_project")
            engines = {e.id: e for e in getattr(project, "ENGINES", ())}
            assert all(isinstance(e, Engine) for e in engines.values()), f"{candidate}: ENGINES must hold Engine"
            return engines
        parent = os.path.dirname(here)
        if parent == here:
            return {}
        here = parent


def _test_files(root):
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")))
        found += [os.path.join(dirpath, f) for f in sorted(filenames) if f.endswith(".py") and f != PROJECT_FILE]
    return found


def scan(root, *, select=None, working_dir="."):
    """Returns (items, engines). Selection is a substring match on the item id."""
    engines = load_project(root)
    items = []
    for path in _test_files(root):
        rel = os.path.relpath(path, working_dir)
        module = _load(path, "ducktest_tests." + rel.replace(os.sep, ".")[:-3])
        for name, obj in vars(module).items():
            test = getattr(obj, "__ducktest__", None)
            if test is None or getattr(obj, "__module__", None) != module.__name__:
                continue
            unknown = [e for e in test.engines if e not in engines]
            if unknown:
                raise UsageError(f"{rel}::{name} needs engines {unknown}, not declared in {PROJECT_FILE}")
            for case_id in test.cases or [None]:
                suffix = f"[{case_id}]" if case_id is not None else ""
                items.append(Item(f"{rel}::{name}{suffix}", test, case_id, rel))
    if select:
        items = [i for i in items if select in i.id]
    return items, engines
