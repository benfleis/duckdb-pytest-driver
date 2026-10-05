"""The test model: a `TestDef` is a definition, a `Test` is what runs.

A test is a definition with one set of parameters applied: one row of the definition's own `params=`
table and one value of every variant axis it takes part in. A definition with no parameters makes
exactly one test. Groups, tags and selection work on tests.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
from dataclasses import dataclass, field

from .binary import UsageError
from .options import Settled
from .resources.client import check_names
from .resources.made import services_in
from .testenv import resolve

SQLLOGIC, PAIRED, PYTHON = "sqllogic", "paired", "python"
BODY_SUFFIXES = (".test", ".test_slow", ".test_coverage")


@dataclass(kw_only=True)
class TestDef(Settled):
    """A `@test` body or a `.test` file, with the options the rules and its own `@test(...)` gave it
    (`options.py`); its tests add their axis values' on top."""

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

    def __post_init__(self):
        self.root_path = self.root_path or self.path


@dataclass
class Test:
    __test__ = False  # not a pytest class

    definition: TestDef
    params_key: str | None  # the row of `definition.params`, or None
    axis_values: dict  # axis name -> Variant
    inputs: tuple = ()  # (ResourceDef, mode): what it binds, with a credential for each storage
    bind: tuple = ()  # (kind, id, source) for each input it binds, by a rule, `@test` or an axis value

    @property
    def id(self):
        parts = ([self.params_key] if self.params_key is not None else []) + [
            f"{a}={v.id}" for a, v in self.axis_values.items()
        ]
        return f"{self.definition.root_path}::{self.definition.name}" + (f"[{','.join(parts)}]" if parts else "")

    @property
    def params(self):
        return self.definition.params.get(self.params_key, {})

    @functools.cached_property
    def options(self):
        """The definition's options with each axis value's on top, in AXES order. An axis value
        gives way to nothing (`Settled.apply`); the axis name is a value too, `{storage}` the id of
        its value."""
        out = self.definition.settled()
        for axis, v in self.axis_values.items():
            where = f"AXES {axis}={v.id}"
            out.give_value(axis, v.id, f"AXES {axis}", axis=self.id, where=where)
            out.apply(v.options, where, axis=self.id)
        if self.params_key is not None:
            for name in check_names(self.params, f"{self.id}: params {self.params_key!r}"):
                where = out.sources.get(("axis_value", name.lower()))
                if where is not None:
                    raise UsageError(
                        f"{self.id}: the value {name!r} is given by {where} and by the params row "
                        f"{self.params_key!r}; give it in one place"
                    )
        return out

    @property
    def tags(self):
        own = list(self.definition.tags)
        return own + [f"type={self.definition.type}"] + [t for t in self.options.tags if t not in own]

    @property
    def why(self):
        """Which place gave what: the rules, `@test`, then the axis values."""
        return self.options.why

    def bind_lines(self):
        return [f"bind: {kind}={ref}  <- {source}" for kind, ref, source in self.bind]

    @property
    def services(self):
        """The ids of the services behind its inputs."""
        return [s.id for s in services_in(d for d, _ in self.inputs)]

    @property
    def engines(self):
        return list(self.options.engines)

    @property
    def mutex(self):
        """The names this test must have to itself; no test that names one of them runs beside it."""
        return list(self.options.mutex)

    @property
    def timeout(self):
        return self.options.timeout

    @property
    def retries(self):
        return self.options.retries

    @property
    def dir_name(self):
        """A directory name that says which test it holds, and is its own: two tests of one file
        name in two directories must not share it. DuckDB reads `[` in a path as a glob and a
        `key=value` segment as a hive partition, so neither is in it."""
        readable = re.sub(
            r"[^A-Za-z0-9._,-]+", "_", self.id.split("::", 1)[-1] + "@" + os.path.basename(self.definition.path)
        )
        return f"{readable}-{hashlib.sha1(self.id.encode()).hexdigest()[:6]}"

    def record(self, project):
        from .leases import engines_of  # it imports this module

        return {
            "identity": {
                "id": self.id,
                "test": f"{self.definition.root_path}::{self.definition.name}",
                "type": self.definition.type,
                "params": self.params_key,
                "variants": {a: v.id for a, v in self.axis_values.items()},
            },
            "selection": {"tags": self.tags},
            "preconditions": {
                "services": self.services,
                "engines": [{"id": d.id, "version": d.version} for d in engines_of(self, project)],
                **({"mutex": self.mutex} if self.mutex else {}),
            },
            "runtime": {
                "params": _plain(self.params),
                # Before provisioning, so what an input gets from a service is not in it.
                "environment": resolve(self).record(),
                "timeout": self.timeout,
                "retries": self.retries,
            },
            "why": self.why,
        }


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
