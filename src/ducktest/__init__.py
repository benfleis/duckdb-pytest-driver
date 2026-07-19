"""The generic pytest driver framework for duckdb test suites.

Distribution: ``duckdb-pytest-driver`` — import package ``ducktest``.
The pytest hooks + harness live in :mod:`ducktest.plugin` and are
auto-registered via the ``pytest11`` entry point (no ``pytest_plugins`` line, no
``sys.path`` hacks). The SQLLogic ``.test`` lane lives in
:mod:`ducktest.sqllogic`.

Public API re-exported here so conftests (and drivers) can
``from ducktest import ...``. Design + docs: see ``docs/`` (README points there).
"""

from .sqllogic import SqlLogicFile  # noqa: F401  (the .test lane)
from .context import get_context  # noqa: F401  (the session context: SessionContext + typed Registry)
from .plugin import (  # noqa: F401  (the harness / plugin)
    register_options,
    find_binary,
    find_duckdb,
    has_driver,
    is_driver,
    run_paired,
    register_broadcast,
    get_broadcast,
    get_store,
    provision_service,
)
from .store import (  # noqa: F401  (the process-shared store: facade acknowledged in the public API)
    ResourceMissing,
    ProvisionFailed,
    ProvisionTimeout,
)
from .requires import requires, requires_matrix, Requirement, collect_requirements  # noqa: F401
from .provision import register_provisioner, get_provisioner, Provisioner, Bindings, State  # noqa: F401
from .suites import (  # noqa: F401  (test-suite declaration API + registry; Phase 0: inert)
    register_suite,
    get_suites,
    credential,
    service,
    use_service,
    Suite,
    Credential,
    Service,
)
from .fixtures import (  # noqa: F401  (the table-spec lane: schema + seed instantiated via duckdb)
    TableSpec,
    register_instantiator,
    get_instantiator,
)
from .steps import step  # noqa: F401
from .sqldef import (  # noqa: F401  (generic multi-statement SQL-def core)
    split_statements,
    sql_literal,
    build_insert,
    run_sql_file,
)

__all__ = [
    "SqlLogicFile",
    "get_context",
    "register_options",
    "find_binary",
    "find_duckdb",
    "has_driver",
    "is_driver",
    "run_paired",
    "register_broadcast",
    "get_broadcast",
    "get_store",
    "provision_service",
    "ResourceMissing",
    "ProvisionFailed",
    "ProvisionTimeout",
    "requires",
    "requires_matrix",
    "Requirement",
    "collect_requirements",
    "register_provisioner",
    "get_provisioner",
    "Provisioner",
    "Bindings",
    "State",
    "register_suite",
    "get_suites",
    "credential",
    "service",
    "use_service",
    "Suite",
    "Credential",
    "Service",
    "TableSpec",
    "register_instantiator",
    "get_instantiator",
    "step",
    "split_statements",
    "sql_literal",
    "build_insert",
    "run_sql_file",
]
