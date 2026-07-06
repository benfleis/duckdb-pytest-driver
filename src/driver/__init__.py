"""Compat alias: ``import driver`` → ``duckdb_pytest_driver``.

The framework's import name is ``duckdb_pytest_driver``. This thin shim re-exports the
same public API (and the ``plugin`` / ``sqllogic`` submodules) under the historical
``driver`` name so existing drivers/conftests that do ``from driver import ...`` keep
working. New code should import ``duckdb_pytest_driver`` directly.
"""

from duckdb_pytest_driver import *  # noqa: F401,F403
from duckdb_pytest_driver import __all__  # noqa: F401
from duckdb_pytest_driver import plugin, sqllogic  # noqa: F401  (so `driver.plugin` resolves)
