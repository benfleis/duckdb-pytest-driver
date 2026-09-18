"""What a test author writes: `@test` on a body, and `Engine` in the project file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

PROJECT_FILE = "ducktest_project.py"


@dataclass(frozen=True)
class Engine:
    """A non-DuckDB SQL engine a body can drive, booted once per run and shared by every test.

    `boot(log_path)` returns a handle with `run(statements)` (as `Client` describes it) and `stop()`. `variant` names
    what was booted (e.g. "spark:4.2:delta-4.4") for the scan record and the report.
    """

    id: str
    variant: str
    boot: Callable


@dataclass(frozen=True)
class Test:
    body: Callable
    engines: tuple = ()
    cases: dict = field(default_factory=dict)
    tags: tuple = ()


def test(*, engines=(), cases=None, tags=()):
    """Mark `body(ctx)` as a test. `cases` maps a case id to the params the body reads as `ctx.case`;
    the test runs once per case."""

    def mark(body):
        body.__ducktest__ = Test(body, tuple(engines), dict(cases or {}), tuple(tags))
        return body

    return mark
