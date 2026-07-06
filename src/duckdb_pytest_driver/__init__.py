"""The generic pytest driver framework for duckdb test suites.

Distribution: ``duckdb-pytest-driver`` — import package ``duckdb_pytest_driver``.
The pytest hooks + harness live in :mod:`duckdb_pytest_driver.plugin` and are
auto-registered via the ``pytest11`` entry point (no ``pytest_plugins`` line, no
``sys.path`` hacks). The SQLLogic ``.test`` lane lives in
:mod:`duckdb_pytest_driver.sqllogic`.

Public API re-exported here so conftests (and drivers) can
``from duckdb_pytest_driver import ...`` (a short ``driver`` compat alias also
re-exports these). Design + docs: see ``docs/`` (README points there).
"""

from .sqllogic import SqlLogicFile  # noqa: F401  (the .test lane)
from .plugin import (  # noqa: F401  (the harness / plugin)
    register_options,
    find_binary,
    has_driver,
    is_driver,
    run_paired,
)
from .requires import requires, Requirement, collect_requirements  # noqa: F401
from .provision import register_provisioner, get_provisioner  # noqa: F401
from .steps import step  # noqa: F401

__all__ = [
    "SqlLogicFile",
    "register_options",
    "find_binary",
    "has_driver",
    "is_driver",
    "run_paired",
    "requires",
    "Requirement",
    "collect_requirements",
    "register_provisioner",
    "get_provisioner",
    "step",
]
