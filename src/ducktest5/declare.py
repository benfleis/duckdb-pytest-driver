"""What a test author writes: `@test` on a body, and `Engine` in the project file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

CONF_FILE = "ducktestconf.py"  # not `ducktest.py`: that name would shadow the package on a sys.path entry


@dataclass(frozen=True)
class Engine:
    """A non-DuckDB SQL engine a body can drive, booted once per run and shared by every test.

    `boot(log_path)` returns a handle with `run(statements)` (as `Client` describes it) and `stop()`.
    `version` names what was booted (e.g. "spark:4.2:delta-4.4") for the scan record and the report.

    `uses` names services the engine can talk to. It never makes a run provision one: when a
    selected test needs such a service, the handle's `use_service(id, block)` is called once, after
    boot, with the facts of the instance.
    """

    id: str
    version: str
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

    `detached` says that an instance keeps running when the process that started it ends: a
    container, a server started with a command. Only such a service can be provisioned in its own
    `ducktest5 provision` invocation and used by a later `exec`; one that lives in this process (a
    thread, an embedded server) is provisioned by the process that runs the tests.

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
    detached: bool = False


@dataclass(frozen=True)
class Variant:
    """One alternative on a variant axis, with everything it brings. The list of what it may hold is
    closed on purpose; what a value gives goes into the case environment, and both kinds of body are
    given it the same way, in this order:

    `requires`   extensions to load before anything else. A setting, a function or a secret type of
                 an extension does not exist until it is loaded.
    `settings`   DuckDB settings, delivered as `SET` after the requires -- never as the binary's own
                 `settings`, which it applies before any extension is there.
    `init_sql`   statements after those: text, or a function of the provisioned services' blocks.
    `values`     what a body writes as `{name}`: a dict, or a function of the blocks. The axis name
                 itself is always a value, so a body can write `{storage}` for the id of this value.
    `roots`      the binary's roots, any of `data_dir`, `local_data_dir`, `temp_dir_root`,
                 `local_temp_dir_root` (a dict, or a function of the blocks). A remote root and its
                 local mirror are one storage seen two ways. A Python body reads them as `ctx.roots`,
                 and `ctx.location()` puts what it makes under `temp_dir_root`.
    `env`        the process environment of the engine that runs the case, and `env_unset` what to
                 take out of it: for what DuckDB or a library inside it reads with getenv (AWS_*,
                 AZURE_*, HTTP_PROXY). The absence of a variable is a fact a literal cannot express.

    `services` and `engines` are what the case then needs, `tags` name it for a selection, and
    `location_on` is the service on which `ctx.location()` puts what the body creates."""

    id: str
    services: tuple = ()
    engines: tuple = ()
    values: object = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    init_sql: object = None
    tags: tuple = ()
    location_on: str | None = None
    requires: tuple = ()
    roots: object = None
    env: dict = field(default_factory=dict)
    env_unset: tuple = ()


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
