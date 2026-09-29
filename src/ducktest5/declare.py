"""What a test author writes: `@test` on a body, and `EngineDef` in the project file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .binary import UsageError

CONF_FILE = "ducktest_conf.py"  # not `ducktest.py`: that name would shadow the package on a sys.path entry


@dataclass(frozen=True)
class EngineDef:
    """A non-DuckDB SQL engine a body can drive, booted once per run and shared by every test.

    `boot(log_path)` returns a handle with `run(statements)` (as `Client` describes it) and `stop()`.
    `version` names what was booted (e.g. "spark:4.2:delta-4.4") for the scan record and the report.

    `uses` names services the engine can talk to. It never makes a run provision one: when a
    selected test needs such a service, the handle's `use_service(id, block)` is called once, after
    boot, with the facts of the instance.

    `dialect` names how a Session on it is given its preamble (`resources/dialect.py`); an engine
    without one takes no inputs through its Session.
    """

    id: str
    version: str
    boot: Callable
    uses: tuple = ()
    dialect: str | None = None


@dataclass(frozen=True)
class ServiceDef:
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

    `resources.ServiceDef` is the same idea on the resource base, as an input of a storage; this one
    serves SERVICES and `provision` until they move over to it.
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
    closed on purpose; what a value gives goes into the test environment, and both kinds of body are
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
    `env`        the process environment of the engine that runs the test, and `env_unset` what to
                 take out of it: for what DuckDB or a library inside it reads with getenv (AWS_*,
                 AZURE_*, HTTP_PROXY). The absence of a variable is a fact a literal cannot express.
                 A name set to None in `env` is unset too. A test that unsets every name a
                 credential delivers does not get that credential, even where its scope matches.

    `services` and `engines` are what the test then needs, `tags` name it for a selection, and
    `location_on` is the service on which `ctx.location()` puts what the body creates. `inputs` names
    STORAGES and CREDENTIALS: a bare id is read-write, `{"id": "ro"}` read-only."""

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
    inputs: tuple = ()


@dataclass(frozen=True)
class TestMark:
    """What `@test(...)` attaches to a body: the author's own words, before the scan gives them a
    path and a name and puts the rules under them in a `TestDef`."""

    __test__ = False  # not a pytest class

    body: Callable
    engines: tuple = ()
    services: tuple = ()
    params: dict = field(default_factory=dict)
    tags: tuple = ()
    variants: tuple = ()
    mutex: tuple = ()
    suites: tuple = ()
    sessions: tuple = ()
    bind: dict = field(default_factory=dict)


def as_mutex(value):
    """`mutex=` takes one name or several."""
    return (value,) if isinstance(value, str) else tuple(value or ())


def test(
    *,
    engines=(),
    services=(),
    params=None,
    tags=(),
    variants=(),
    mutex=(),
    suites=(),
    sessions=(),
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

    `sessions` names the project's SESSIONS the body drives, as `ctx.session(name)`. A test that
    names no DuckDB one gets a default DuckDB Session.

    `bind` maps a role to one of the project's STORAGES or CREDENTIALS, by id; it replaces what a
    rule binds to the same role."""
    if "cases" in unknown:
        raise UsageError("@test(cases=...) is now @test(params=...); the body reads its row as ctx.params")
    if unknown:
        raise TypeError(f"test() got unexpected keyword arguments {sorted(unknown)}")

    def mark(body):
        body.__ducktest__ = TestMark(
            body,
            tuple(engines),
            tuple(services),
            dict(params or {}),
            tuple(tags),
            tuple(variants),
            as_mutex(mutex),
            tuple(suites),
            tuple(sessions),
            dict(bind or {}),
        )
        return body

    return mark
