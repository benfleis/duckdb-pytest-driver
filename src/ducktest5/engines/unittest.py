"""The `unittest` binary as an engine: the process of one `.test` batch."""

from __future__ import annotations

from dataclasses import dataclass

from ..resources.engine import BATCH, EngineDef


@dataclass(eq=False)
class UnittestEngineDef(EngineDef):
    """The engine of a `.test` batch. Its process is given one test environment, as a config and an
    init file, and `environment` is that environment's key: the content that makes the identity of
    the instance, so two `.test` bodies whose environments are equal lease one batch. The process is
    started and ended by the batch runner (`sqllogic.run_batch`), not opened here."""

    kind = "unittest"
    dialect = "unittest"

    scope: str = BATCH
    environment: str = ""
