"""Engines: a service a test drives with SQL. It is declared among SERVICES, and the run does not
provision it as it does the others: a test that names it gets it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .base import NEVER
from .service import ServiceDef


@dataclass(eq=False)
class EngineDef(ServiceDef):
    """An engine booted once per run and shared by every test. `boot(log_path)` returns a handle
    with `run(statements)` (as `Client` describes it) and `stop()`. `version` names what was booted
    (e.g. "spark:4.2:delta-4.4") for the scan record and the report.

    `dialect` names how a Session on it is given its preamble (`resources/dialect.py`), the
    storages and credentials a test binds among it; an engine without one takes no inputs through
    its Session."""

    kind = "engine"
    provisioned = False

    id: str
    version: str = ""
    boot: Callable | None = None
    dialect: str | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = NEVER
