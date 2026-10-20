"""What runs a `.test` batch, as an engine: the `unittest` binary, one process for the batch."""

from __future__ import annotations

from dataclasses import dataclass

from ..resources.dialect import DUCK_SQLLOGIC
from ..resources.engine import BATCH, EngineDef


@dataclass(eq=False)
class UnittestEngineDef(EngineDef):
    """The engine of a `.test` batch. Its process is given one test environment, as a config and an
    init file, and `environment` is that environment's key: the content that makes the identity of
    the instance, so two `.test` bodies whose environments are equal lease one batch. The process is
    started and ended by the batch runner (`sqllogic.run_batch`), not opened here."""

    kind = dialect = DUCK_SQLLOGIC

    lifetime: str = BATCH
    environment: str = ""
