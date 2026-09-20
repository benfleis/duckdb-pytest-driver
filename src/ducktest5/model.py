"""The test model: a test is a definition, a case is what runs.

A case is a test with one set of parameters applied: one entry of the test's own `cases=` table and one
value of every variant axis the test takes part in. A test with no parameters has exactly one case.
Groups, tags and selection work on cases.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SQLLOGIC, PAIRED, PYTHON = "sqllogic", "paired", "python"
BODY_SUFFIXES = (".test", ".test_slow", ".test_coverage")


@dataclass
class TestDef:
    path: str  # relative to the working directory; for a .test body also its name in the unittest binary
    name: str
    type: str
    body: object = None  # the Python callable, for python and paired tests
    body_file: str | None = None  # the .test file, for sqllogic and paired tests
    cases: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    services: list = field(default_factory=list)
    engines: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    in_default_set: bool = True
    timeout: float | None = None
    why: list = field(default_factory=list)


@dataclass
class Case:
    test: TestDef
    case_id: str | None
    axis_values: dict  # axis name -> Variant

    @property
    def id(self):
        parts = ([self.case_id] if self.case_id is not None else []) + [
            f"{a}={v.id}" for a, v in self.axis_values.items()
        ]
        return f"{self.test.path}::{self.test.name}" + (f"[{','.join(parts)}]" if parts else "")

    @property
    def params(self):
        return self.test.cases.get(self.case_id, {})

    @property
    def tags(self):
        tags = list(self.test.tags) + [f"type={self.test.type}"]
        for v in self.axis_values.values():
            tags += [t for t in v.tags if t not in tags]
        return tags

    def _with_variants(self, own, kind):
        out = list(own)
        for v in self.axis_values.values():
            out += [x for x in getattr(v, kind) if x not in out]
        return out

    @property
    def services(self):
        return self._with_variants(self.test.services, "services")

    @property
    def engines(self):
        return self._with_variants(self.test.engines, "engines")

    @property
    def values(self):
        merged = dict(self.test.values)
        for v in self.axis_values.values():
            merged.update(v.values)
        return merged

    @property
    def settings(self):
        merged = dict(self.test.settings)
        for v in self.axis_values.values():
            merged.update(v.settings)
        return merged

    @property
    def dir_name(self):
        """A directory name that says which case it holds."""
        return re.sub(
            r"[^A-Za-z0-9._=,\[\]-]+", "_", self.id.split("::", 1)[-1] + "@" + os.path.basename(self.test.path)
        )

    def record(self, project):
        return {
            "identity": {
                "id": self.id,
                "test": f"{self.test.path}::{self.test.name}",
                "type": self.test.type,
                "case": self.case_id,
                "variants": {a: v.id for a, v in self.axis_values.items()},
            },
            "selection": {"tags": self.tags, "in_default_set": self.test.in_default_set},
            "preconditions": {
                "services": self.services,
                "engines": [{"id": e, "variant": project.engines[e].variant} for e in self.engines],
            },
            "runtime": {
                "case": _plain(self.params),
                "values": _plain(self.values),
                "settings": self.settings,
                "timeout": self.test.timeout,
            },
            "why": self.test.why,
        }


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
