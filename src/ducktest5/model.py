"""The test model: a test is a definition, a case is what runs.

A case is a test with one set of parameters applied: one entry of the test's own `cases=` table and one
value of every variant axis the test takes part in. A test with no parameters has exactly one case.
Groups, tags and selection work on cases.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from .caseenv import resolve

SQLLOGIC, PAIRED, PYTHON = "sqllogic", "paired", "python"
BODY_SUFFIXES = (".test", ".test_slow", ".test_coverage")


@dataclass
class TestDef:
    path: str  # relative to the working directory; for a .test body also its name in the unittest binary
    name: str
    type: str
    # Relative to the project root, so that a case id is the same from every working directory. The
    # binary and the PATH arguments keep using `path`, which is relative to the working directory.
    root_path: str | None = None
    body: object = None  # the Python callable, for python and paired tests
    body_file: str | None = None  # the .test file, for sqllogic and paired tests
    cases: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    services: list = field(default_factory=list)
    engines: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    suites: list = field(default_factory=list)  # a suite this test declares itself in, or out of as `!NAME`
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    timeout: float | None = None
    retries: int = 0  # how often a FAILED or ERROR may run again
    why: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)  # (field, key) -> the rule that gave that one entry

    def __post_init__(self):
        self.root_path = self.root_path or self.path

    def source_of(self, field_name, key):
        """Which rule gave one entry; what no rule gave, the test declared itself."""
        return self.sources.get((field_name, key), "@test")


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
        return f"{self.test.root_path}::{self.test.name}" + (f"[{','.join(parts)}]" if parts else "")

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
    def dir_name(self):
        """A directory name that says which case it holds, and is its own: two tests of one file
        name in two directories must not share it."""
        readable = re.sub(
            r"[^A-Za-z0-9._=,\[\]-]+", "_", self.id.split("::", 1)[-1] + "@" + os.path.basename(self.test.path)
        )
        return f"{readable}-{hashlib.sha1(self.id.encode()).hexdigest()[:6]}"

    def record(self, project):
        return {
            "identity": {
                "id": self.id,
                "test": f"{self.test.root_path}::{self.test.name}",
                "type": self.test.type,
                "case": self.case_id,
                "variants": {a: v.id for a, v in self.axis_values.items()},
            },
            "selection": {"tags": self.tags, **({"suites": list(self.test.suites)} if self.test.suites else {})},
            "preconditions": {
                "services": self.services,
                "engines": [{"id": e, "version": project.engines[e].version} for e in self.engines],
            },
            "runtime": {
                "case": _plain(self.params),
                # Before provisioning, so what a variant makes out of a service's block is not in it.
                "environment": resolve(self).record(),
                "timeout": self.test.timeout,
                "retries": self.test.retries,
            },
            "why": self.test.why,
        }


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
