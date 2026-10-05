"""What a test author writes: `@test` on a body, and a `Variant` of an axis in the project file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


CONF_FILE = "ducktest_conf.py"  # not `ducktest.py`: that name would shadow the package on a sys.path entry


@dataclass(frozen=True)
class Variant:
    """One alternative on a variant axis, with everything it brings. The list of what it may hold is
    closed on purpose; what a value gives goes into the test environment, and both kinds of body are
    given it the same way, in this order:

    `requires`   extensions to load before anything else. A setting, a function or a secret type of
                 an extension does not exist until it is loaded.
    `settings`   DuckDB settings, delivered as `SET` after the requires -- never as the binary's own
                 `settings`, which it applies before any extension is there.
    `init_sql`   statements after those.
    `values`     what a body writes as `{name}`. The axis name itself is always a value, so a body
                 can write `{storage}` for the id of this value. A name given here, or the axis
                 name, that a rule's `values=`, `@test` or the params row gives too is an error.
    `roots`      the binary's roots, any of `data_dir`, `local_data_dir`, `temp_dir_root`,
                 `local_temp_dir_root`. A remote root and its local mirror are one storage seen two
                 ways. A Python body reads them as `ctx.values.temp_dir` (its own place under
                 `temp_dir_root`), `ctx.values.data_dir` and their `local_` twins.
    `process_env`  the process environment of the engine that runs the test: a value sets a
                 variable, None unsets it. For what DuckDB or a library inside it reads with getenv
                 (HTTP_PROXY, ...); the absence of a variable is a fact a literal cannot express.
                 Keys come from a credential, never from here.

    `engines` are what the test then needs, and `tags` name it for a selection. `inputs` names
    STORAGES, CREDENTIALS and SERVICES: a bare id is read-write, `{"id": "ro"}` read-only; a bound
    storage is where `ctx.location()` puts what the body creates. Each binds the role its kind names
    (`storage`, `credential`), replacing what a `default()` binds to that role; a `match()` or
    `@test` binding of the same role is an error."""

    id: str
    engines: tuple = ()
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    init_sql: str | None = None
    tags: tuple = ()
    requires: tuple = ()
    roots: dict = field(default_factory=dict)
    process_env: dict = field(default_factory=dict)
    inputs: tuple = ()


@dataclass(frozen=True)
class TestMark:
    """What `@test(...)` attaches to a body: the author's own words, before the scan gives them a
    path and a name and puts the rules under them in a `TestDef`."""

    __test__ = False  # not a pytest class

    body: Callable
    engines: tuple = ()
    params: dict = field(default_factory=dict)
    tags: tuple = ()
    variants: tuple = ()
    mutex: tuple = ()
    suites: tuple = ()
    bind: dict = field(default_factory=dict)


def as_mutex(value):
    """`mutex=` takes one name or several."""
    return (value,) if isinstance(value, str) else tuple(value or ())


def test(
    *,
    engines=(),
    params=None,
    tags=(),
    variants=(),
    mutex=(),
    suites=(),
    bind=None,
    **unknown,
):
    """Mark `body(ctx)` as a test definition. `params` maps a key to the params the body reads as
    `ctx.params`; the key is the first part of the test id. `variants` names the project's axes the
    definition takes part in. It makes one test for each combination of a params row and one value
    of every axis.

    `mutex` names what each of its tests must have to itself: a name, or several. Two tests that
    name one of them never run at the same time, however many workers the run has.

    `suites` says which suites its tests are in: `"smoke"` declares them in whatever the suite's own
    definition selects, `"!smoke"` declares them out and nothing but `--id` puts one back.

    `engines` names the engines among the project's ENGINES the body drives. Each gets a Client
    of its own, `ctx.client(name)`; a test that names no DuckDB engine gets a Client on the default
    one: the project's DuckDB engine when it declares exactly one, else the built-in one.

    `bind` maps a role to one of the project's STORAGES, CREDENTIALS or SERVICES, by id; it
    replaces what a rule binds to the same role. An axis value that binds that role too is an
    error."""
    if unknown:
        raise TypeError(f"test() got unexpected keyword arguments {sorted(unknown)}")

    def mark(body):
        body.__ducktest__ = TestMark(
            body,
            tuple(engines),
            dict(params or {}),
            tuple(tags),
            tuple(variants),
            as_mutex(mutex),
            tuple(suites),
            dict(bind or {}),
        )
        return body

    return mark
