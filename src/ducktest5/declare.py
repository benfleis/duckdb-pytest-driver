"""What a test author writes: `@test` on a body, and `Engine` in the project file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

CONF_FILE = "ducktestconf.py"  # not `ducktest.py`: that name would shadow the package on a sys.path entry


@dataclass(frozen=True)
class Engine:
    """A non-DuckDB SQL engine a body can drive, booted once per run and shared by every test.

    `boot(log_path)` returns a handle with `run(statements)` (as `Client` describes it) and `stop()`. `variant` names
    what was booted (e.g. "spark:4.2:delta-4.4") for the scan record and the report.

    `uses` names services the engine can talk to. It never makes a run provision one: when a
    selected test needs such a service, the handle's `use_service(id, block)` is called once, after
    boot, with the facts of the instance.
    """

    id: str
    variant: str
    boot: Callable
    uses: tuple = ()


@dataclass(frozen=True)
class Service:
    """Infrastructure a test needs and the run provides: an object store, a catalog server.

    `block(**overrides)` gives the facts of one instance (endpoint, bucket, credentials) as a plain
    dict, the same shape whether the run started the instance or found it. The rest work on a block:
    `alive(block)` is a cheap probe; `start(block)` / `stop(block)` boot and remove a managed
    instance (`start=None`: the service is never ours to start, e.g. a cloud workspace);
    `setup(block)` makes the structure tests rely on (a bucket, a user) and must be idempotent, as
    it runs in every mode; `clean(block, prefix)` removes what a test wrote under `prefix`.
    `secrets` names block fields that must not reach a report.

    How an instance comes to be is the run's choice, not the test's -- see `provision`.
    """

    id: str
    block: Callable
    alive: Callable
    start: Callable | None = None
    stop: Callable | None = None
    setup: Callable | None = None
    clean: Callable | None = None
    location: Callable | None = None  # (block, *parts) -> a writable URI under the run's prefix
    secrets: tuple = ()


@dataclass(frozen=True)
class Variant:
    """One alternative on a variant axis, with everything it brings. The list of what it may hold is
    closed on purpose: services and engines the case then needs, `values` for `{name}` in SQL,
    DuckDB `settings`, `init_sql` to run before the body (text, or a function of the provisioned
    services' blocks), `tags`, and `location_on`: the service on which `ctx.location()` puts what the
    body creates."""

    id: str
    services: tuple = ()
    engines: tuple = ()
    values: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    init_sql: object = None
    tags: tuple = ()
    location_on: str | None = None


@dataclass(frozen=True)
class Test:
    body: Callable
    engines: tuple = ()
    services: tuple = ()
    cases: dict = field(default_factory=dict)
    tags: tuple = ()
    variants: tuple = ()


def test(*, engines=(), services=(), cases=None, tags=(), variants=()):
    """Mark `body(ctx)` as a test. `cases` maps a case id to the params the body reads as `ctx.case`;
    `variants` names the project's axes the test takes part in. The test runs once for each
    combination of a case and one value of every axis."""

    def mark(body):
        body.__ducktest__ = Test(body, tuple(engines), tuple(services), dict(cases or {}), tuple(tags), tuple(variants))
        return body

    return mark
