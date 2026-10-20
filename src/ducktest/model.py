"""The test model: a `TestDef` is a definition, a `Test` is what runs.

A test is one cell of its definition's matrix: one value of every parameter the definition runs
over, from the project's matrix or its own `matrix=`. A definition with no parameters makes exactly
one test. Groups, tags and selection work on tests.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
from dataclasses import dataclass, field

from .binary import UsageError
from .options import BINDINGS, Options, Settled
from .resources.client import check_names
from .resources.made import services_in
from .testenv import resolve

SQLLOGIC, PAIRED, PYTHON = "sqllogic", "paired", "python"
BODY_SUFFIXES = (".test", ".test_slow", ".test_coverage")


@dataclass(kw_only=True)
class TestDef(Settled):
    """A `@test` body or a `.test` file, with the options the rules and its own `@test(...)` gave it
    (`options.py`); its tests add the matrix values of their cell on top."""

    __test__ = False  # not a pytest class
    path: str  # relative to the working directory; for a .test body also its name in the unittest binary
    name: str
    type: str
    # Relative to the project root, so that a test id is the same from every working directory. The
    # binary and the PATH arguments keep using `path`, which is relative to the working directory.
    root_path: str | None = None
    body: object = None  # the Python callable, for python and paired tests
    body_file: str | None = None  # the .test file, for sqllogic and paired tests

    def __post_init__(self):
        self.root_path = self.root_path or self.path


@dataclass(frozen=True)
class Value:
    """The value `name` of one parameter. A project parameter's value holds slots (`options`); a
    parameter a rule or `@test` defines holds free data for the body (`data`)."""

    parameter: str
    name: str
    options: Options | None = None
    data: object = None

    @property
    def where(self):
        return f"matrix {self.parameter}={self.name}"

    @property
    def param(self):
        """What `ctx.params.<parameter>` is: the data of a local value, the name of a project one."""
        return self.name if self.options is not None else self.data


class Params(dict):
    """Every parameter of a cell by name, `params.case` or `params["case"]`."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"no parameter {name!r}; this cell has {sorted(self)}") from None


@dataclass
class Test:
    __test__ = False  # not a pytest class

    definition: TestDef
    cell: dict = field(default_factory=dict)  # parameter name -> Value, in the order of `definition.matrix`
    inputs: tuple = ()  # (ResourceDef, mode): what it binds, with a credential for each storage
    bind: tuple = ()  # (slot, name, source) for each input it binds, by a rule, `@test` or a matrix value
    roles: dict = field(default_factory=dict)  # storage, data, temp -> (StorageDef, `.at()` path or None)

    @property
    def name(self):
        parts = [f"{p}={v.name}" for p, v in self.cell.items()]
        return f"{self.definition.root_path}::{self.definition.name}" + (f"[{','.join(parts)}]" if parts else "")

    @property
    def params(self):
        return Params({p: v.param for p, v in self.cell.items()})

    @functools.cached_property
    def options(self):
        """The definition's options with the cell's values on top, in the order of its parameters.
        They give way to nothing (`Settled.apply`). A project parameter's name is a value, `{storage}`
        the name of its value; a local value's fields are values, or, if it is no dict, the value
        itself under the parameter's name."""
        out = self.definition.settled()
        for p, v in self.cell.items():
            if v.options is not None:
                out.give_value(p, v.name, f"matrix {p}", cell=self.name, where=v.where)
                out.apply(v.options, v.where, cell=self.name)
                continue
            fields = v.data if isinstance(v.data, dict) else {p: v.data}
            if slot := next((n for n in fields if n in BINDINGS), None):
                raise UsageError(
                    f"{self.name}: {v.where}: {slot!r} is a binding slot, and a body's own matrix value gives only "
                    f"plain values; bind the project's matrix parameter (matrix=[{slot!r}]) or bind directly "
                    f"(@test({slot}={fields[slot]!r}))"
                )
            for name, value in check_names(fields, f"{self.name}: {v.where}").items():
                out.give_value(name, value, v.where, cell=self.name)
        return out

    @property
    def tags(self):
        own = list(self.definition.tags)
        return own + [f"type={self.definition.type}"] + [t for t in self.options.tags if t not in own]

    @property
    def why(self):
        """Which place gave what: the rules, `@test`, then the cell's matrix values."""
        return self.options.why

    def bind_lines(self):
        return [f"bind: {kind}={ref}  <- {source}" for kind, ref, source in self.bind]

    @property
    def services(self):
        """The ids of the services behind its inputs."""
        return [s.name for s in services_in(d for d, _ in self.inputs)]

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
            r"[^A-Za-z0-9._,-]+", "_", self.name.split("::", 1)[-1] + "@" + os.path.basename(self.definition.path)
        )
        return f"{readable}-{hashlib.sha1(self.name.encode()).hexdigest()[:6]}"

    def record(self, project):
        from .leases import engines_of  # it imports this module

        return {
            "identity": {
                "name": self.name,
                "test": f"{self.definition.root_path}::{self.definition.name}",
                "type": self.definition.type,
                "matrix": {p: v.name for p, v in self.cell.items()},
            },
            "selection": {"tags": self.tags},
            "preconditions": {
                "services": self.services,
                "storages": [d.name for d, _ in self.inputs if d.kind == "storage"],
                "engines": [{"name": d.name, "version": d.version} for d in engines_of(self, project)],
                **({"mutex": self.mutex} if self.mutex else {}),
            },
            "runtime": {
                "params": _plain(dict(self.params)),
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
