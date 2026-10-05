"""What a test author writes: `@test` on a body, and a `Variant` of an axis in the project file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .options import Options


CONF_FILE = "ducktest_conf.py"  # not `ducktest.py`: that name would shadow the package on a sys.path entry


@dataclass(frozen=True, init=False)
class Variant:
    """One value of an axis, with everything it brings: the options a rule and `@test` take
    (`options.py`), but `axes` and `suites`. What it gives goes into the test environment, and
    both kinds of body are given it the same way:

    `requires`     extensions to load before anything else. A setting, a function or a secret type
                   of an extension does not exist until it is loaded.
    `settings`     DuckDB settings, delivered as `SET` after the requires -- never as the binary's
                   own `settings`, which it applies before any extension is there.
    `init_sql`     statements after those.
    `values`       what a body writes as `{name}`. The axis name itself is always a value, so a body
                   can write `{storage}` for the id of this value.
    `process_env`  the process environment of the engine that runs the test: a value sets a
                   variable, None unsets it. For what DuckDB or a library inside it reads with
                   getenv (HTTP_PROXY, ...); the absence of a variable is a fact a literal cannot
                   express. Keys come from a credential, never from here.
    `storage`, `credential`, `service`  bind one of STORAGES, CREDENTIALS, SERVICES by id; a bound
                   storage is where `{TEMP_DIR}` and `ctx.location()` are.

    A value, or a binding, that a rule, `@test`, the params row or another axis value gives too is
    an error; only what a `default()` binds gives way to it."""

    id: str
    options: Options

    def __init__(self, id, **options):
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "options", Options.of(options, f"Variant {id!r}", leave_out=("axes", "suites")))


@dataclass(frozen=True)
class TestMark:
    """What `@test(...)` attaches to a body: the author's own words, before the scan gives them a
    path and a name and puts the rules under them in a `TestDef`."""

    __test__ = False  # not a pytest class

    body: Callable
    params: dict
    options: Options


def test(*, params=None, **options):
    """Mark `body(ctx)` as a test definition. `params` maps a key to the params the body reads as
    `ctx.params`; the key is the first part of the test id. The rest are the options a rule takes
    (`options.py`), and they win over the rules'.

    `axes` names the project's axes the definition takes part in. It makes one test for each
    combination of a params row and one value of every axis.

    `engines` names the engines among the project's ENGINES the body drives. Each gets a Client
    of its own, `ctx.client(name)`; a test that names no DuckDB engine gets a Client on the default
    one: the project's DuckDB engine when it declares exactly one, else the built-in one.

    `storage`, `credential` and `service` bind one of the project's STORAGES, CREDENTIALS or
    SERVICES, by id; each replaces what a rule binds to the same kind. An axis value that binds
    that kind too is an error."""

    def mark(body):
        where = f"{os.path.basename(body.__code__.co_filename)}::{body.__name__}: @test(...)"
        body.__ducktest__ = TestMark(body, dict(params or {}), Options.of(options, where))
        return body

    return mark
