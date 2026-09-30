"""The test model: a `TestDef` is a definition, a `Test` is what runs.

A test is a definition with one set of parameters applied: one row of the definition's own `params=`
table and one value of every variant axis it takes part in. A definition with no parameters makes
exactly one test. Groups, tags and selection work on tests.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from .resources.made import services_in
from .testenv import resolve

SQLLOGIC, PAIRED, PYTHON = "sqllogic", "paired", "python"
BODY_SUFFIXES = (".test", ".test_slow", ".test_coverage")


@dataclass
class TestDef:
    """A `@test` body or a `.test` file, after the rules. Its timeout, retries, mutex and suites
    apply to every test made from it."""

    __test__ = False  # not a pytest class
    path: str  # relative to the working directory; for a .test body also its name in the unittest binary
    name: str
    type: str
    # Relative to the project root, so that a test id is the same from every working directory. The
    # binary and the PATH arguments keep using `path`, which is relative to the working directory.
    root_path: str | None = None
    body: object = None  # the Python callable, for python and paired tests
    body_file: str | None = None  # the .test file, for sqllogic and paired tests
    params: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    engines: list = field(default_factory=list)  # ids of the engines it drives, among the project's ENGINES
    variants: list = field(default_factory=list)
    mutex: list = field(default_factory=list)  # what each of its tests must have to itself while it runs
    suites: list = field(default_factory=list)  # a suite it declares itself in, or out of as `!NAME`
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    bind: dict = field(default_factory=dict)  # role -> the id of a storage, credential or service
    timeout: float | None = None
    retries: int = 0  # how often a FAILED or ERROR may run again
    why: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)  # (field, key) -> the rule that gave that one entry

    def __post_init__(self):
        self.root_path = self.root_path or self.path

    def source_of(self, field_name, key):
        """Which rule gave one entry; what no rule gave, the definition declared itself."""
        return self.sources.get((field_name, key), "@test")


@dataclass
class Test:
    __test__ = False  # not a pytest class

    definition: TestDef
    params_key: str | None  # the row of `definition.params`, or None
    axis_values: dict  # axis name -> Variant
    inputs: tuple = ()  # (ResourceDef, mode): what it binds and its axis values' inputs, with a credential for each storage
    bind: tuple = ()  # (role, id, source) for each input it binds, by a rule, `@test` or an axis value

    @property
    def id(self):
        parts = ([self.params_key] if self.params_key is not None else []) + [
            f"{a}={v.id}" for a, v in self.axis_values.items()
        ]
        return f"{self.definition.root_path}::{self.definition.name}" + (f"[{','.join(parts)}]" if parts else "")

    @property
    def params(self):
        return self.definition.params.get(self.params_key, {})

    @property
    def tags(self):
        tags = list(self.definition.tags) + [f"type={self.definition.type}"]
        for v in self.axis_values.values():
            tags += [t for t in v.tags if t not in tags]
        return tags

    def _with_variants(self, own, kind):
        out = list(own)
        for v in self.axis_values.values():
            out += [x for x in getattr(v, kind) if x not in out]
        return out

    @property
    def why(self):
        """Which rule gave what, then what an axis value binds."""
        return self.definition.why + [
            f"bind={ {role: ref}!r}  <- {source}"
            for role, ref, source in self.bind
            if source != self.definition.source_of("bind", role)
        ]

    def bind_lines(self):
        return [f"bind: {role}={ref}  <- {source}" for role, ref, source in self.bind]

    @property
    def services(self):
        """The ids of the services behind its inputs."""
        return [s.id for s in services_in(d for d, _ in self.inputs)]

    @property
    def engines(self):
        return self._with_variants(self.definition.engines, "engines")

    @property
    def mutex(self):
        """The names this test must have to itself; no test that names one of them runs beside it."""
        return list(self.definition.mutex)

    @property
    def dir_name(self):
        """A directory name that says which test it holds, and is its own: two tests of one file
        name in two directories must not share it."""
        readable = re.sub(
            r"[^A-Za-z0-9._=,\[\]-]+", "_", self.id.split("::", 1)[-1] + "@" + os.path.basename(self.definition.path)
        )
        return f"{readable}-{hashlib.sha1(self.id.encode()).hexdigest()[:6]}"

    def record(self, project):
        return {
            "identity": {
                "id": self.id,
                "test": f"{self.definition.root_path}::{self.definition.name}",
                "type": self.definition.type,
                "params": self.params_key,
                "variants": {a: v.id for a, v in self.axis_values.items()},
            },
            "selection": {
                "tags": self.tags,
                **({"suites": list(self.definition.suites)} if self.definition.suites else {}),
            },
            "preconditions": {
                "services": self.services,
                "engines": [{"id": e, "version": project.engines[e].version} for e in self.engines],
                **({"mutex": self.mutex} if self.mutex else {}),
            },
            "runtime": {
                "params": _plain(self.params),
                # Before provisioning, so what an input gets from a service is not in it.
                "environment": resolve(self).record(),
                "timeout": self.definition.timeout,
                "retries": self.definition.retries,
            },
            "why": self.why,
        }


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
