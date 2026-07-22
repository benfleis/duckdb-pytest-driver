"""Demo backend: wire the pure-DuckDB fixture instantiator + a `resources` fixture.

This is the smallest possible consumer of the table-fixture scaffolding. It shows the
shape a real backend follows:

  * register an Instantiator (here the built-in `DuckDBInstantiator`) from a scoped
    conftest, exactly like a provisioner (driver/provision.py);
  * a `resources` fixture reads the test's `@requires(source=Fixture(...))`, asks the
    instantiator to instantiate each definition, and hands the test the resulting tables.

A REAL backend differs in only two places: (1) it resolves the duckdb shell from the
build path via `find_duckdb(config, working_dir)` rather than PATH, and (2) its
instantiator is a Databricks/Iceberg one that applies the 2x2 storage properties. The
fixture files and the test bodies do not change.
"""

import os
import pathlib
import shutil
import tempfile

import pytest

from duckdb_pytest_driver import Fixture, collect_requirements, register_instantiator
from duckdb_pytest_driver.fixtures import DuckDBInstantiator, load_fixture

_HERE = pathlib.Path(__file__).parent
_FIXTURES = [_HERE / "fixtures"]


def _duckdb_shell():
    # Demo locator: env override, else PATH. (A real consumer derives it from the
    # build path via fixtures.duckdb_shell_for(find_binary(config, working_dir)).)
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


def pytest_configure(config):
    register_instantiator(config, DuckDBInstantiator(), scope=str(_HERE))


class Resources:
    """What a test sees: the instantiated tables + a duckdb db holding them all."""

    def __init__(self, db, tables):
        self.db = db
        self.tables = tables  # {name: Table}
        # env a `.test` body would read (parity with the Databricks resources.env).
        self.env = {"DEMO_DUCKDB": db}


@pytest.fixture
def resources(request):
    """Instantiate every `@requires(source=Fixture(...))` on the test into one duckdb db."""
    shell = _duckdb_shell()
    if not shell:
        pytest.skip("no duckdb shell found (set $DUCKDB_SHELL or put `duckdb` on PATH)")

    instantiator = DuckDBInstantiator()
    tmp = tempfile.mkdtemp(prefix="fixdemo.")
    db = os.path.join(tmp, "demo.duckdb")
    tables = {}
    for req in collect_requirements(request.node):
        if not isinstance(req.source, Fixture):
            continue  # this demo only handles Fixture refs
        definition = load_fixture(req.source, _FIXTURES)
        table = instantiator.instantiate(definition, db, duckdb_bin=shell, seed=req.source.seed)
        tables[table.name] = table
    try:
        yield Resources(db, tables)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
