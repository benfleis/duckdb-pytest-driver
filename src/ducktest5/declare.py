"""What a test author writes: `@test` on a body."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .options import Options


CONF_FILE = "ducktest_conf.py"  # not `ducktest.py`: that name would shadow the package on a sys.path entry


@dataclass(frozen=True)
class TestMark:
    """What `@test(...)` attaches to a body: the author's own words, before the scan gives them a
    path and a name and puts the rules under them in a `TestDef`."""

    __test__ = False  # not a pytest class

    body: Callable
    options: Options


def test(**options):
    """Mark `body(ctx)` as a test definition. The options are the ones a rule takes (`options.py`),
    and they win over the rules'.

    `matrix` lists the parameters the definition runs over: the name of one of the project's
    MATRIX parameters, or a dict that defines parameters of its own, `{"case": CASES}`, whose values
    are free data the body reads as `ctx.params.case`. It makes one test for each cell: each
    combination of one value of every parameter.

    `engines` names the engines among the project's ENGINES the body drives. Each gets a Client
    of its own, `ctx.client(name)`; a test that names no DuckDB engine gets a Client on the default
    one: the project's DuckDB engine when it declares exactly one, else the built-in one.

    `storage`, `credential` and `service` bind one of the project's STORAGES, CREDENTIALS or
    SERVICES, by id; each replaces what a rule binds to the same kind. A MATRIX value that binds
    that kind too is an error."""

    def mark(body):
        where = f"{os.path.basename(body.__code__.co_filename)}::{body.__name__}: @test(...)"
        body.__ducktest__ = TestMark(body, Options.of(options, where))
        return body

    return mark
