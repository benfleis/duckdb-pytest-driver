"""Self-tests for the table-fixture scaffolding (fixtures.py).

Two layers:
  * pure-parse tests (no duckdb) — header parsing, list parsing, File stems, the
    type-map helper;
  * middleman tests (need a duckdb CLI) — instantiate a fixture and read back its
    resolved schema + rows. Skipped cleanly if no `duckdb` is on PATH / $DUCKDB_CLI.
"""

import os
import shutil

import pytest

from ducktest.fixtures import (
    _UNSET,
    Column,
    DuckDBInstantiator,
    Fixture,
    FixtureError,
    Table,
    canonicalize,
    load_fixture,
    map_columns,
    parse_fixture,
    resolve_seed,
)


def _cli():
    return os.environ.get("DUCKDB_CLI") or shutil.which("duckdb")


needs_duckdb = pytest.mark.skipif(_cli() is None, reason="no duckdb CLI (set $DUCKDB_CLI or PATH)")


# --- pure parse / helpers (no duckdb) --------------------------------------


def test_fixture_ref_is_name_only_and_io_free():
    # A ref is a pure value: no path, no file read, `.sql` tolerated + stripped.
    assert Fixture("simple_table").name == "simple_table"
    assert Fixture("simple_table.sql").name == "simple_table"
    # Referencing a non-existent fixture must NOT touch the filesystem (lazy).
    assert Fixture("does_not_exist_anywhere").name == "does_not_exist_anywhere"
    with pytest.raises(ValueError):
        Fixture("")


def test_parse_header_and_body():
    text = "-- fixture: t\n-- keys: [id, name]\nCREATE TABLE t (id INT);\n"
    fx = parse_fixture(text, "some/t.sql")
    assert fx.name == "t"
    assert fx.table() == "t"
    assert fx.keys() == ["id", "name"]
    assert fx.body == text  # body is the whole file (comments are valid SQL)


def test_parse_name_defaults_to_stem():
    fx = parse_fixture("CREATE TABLE whatever (id INT);\n", "x/whatever.sql")
    assert fx.name == "whatever"


def test_fixture_seed_override_is_pure():
    f = Fixture("id_name")
    assert f.seed is _UNSET
    assert f.Seed(None).seed is None
    assert f.Seed([(1, "a")]).seed == [(1, "a")]
    assert f.Seed(None).name == "id_name"
    assert f.seed is _UNSET  # frozen: original untouched


def test_resolve_seed():
    assert resolve_seed(_UNSET, [(1,)]) == [(1,)]  # not overridden -> fixture's own
    assert resolve_seed(None, [(1,)]) == []  # None -> empty
    assert resolve_seed([(9,)], [(1,)]) == [(9,)]  # list -> replace


def test_map_columns_maps_and_errors():
    t = Table(
        "t",
        [Column("id", "INTEGER"), Column("amt", "DECIMAL(10,2)"), Column("nm", "VARCHAR")],
        seed_data=[],
    )
    spark = {"INTEGER": "INT", "DECIMAL": "DECIMAL", "VARCHAR": "STRING"}
    assert map_columns(t, spark) == [("id", "INT"), ("amt", "DECIMAL(10,2)"), ("nm", "STRING")]
    with pytest.raises(FixtureError):
        map_columns(t, {"INTEGER": "INT"})  # missing DECIMAL/VARCHAR -> loud
    # passthrough keeps the duckdb spelling instead of failing
    assert map_columns(t, {"INTEGER": "INT"}, on_missing="passthrough")[2] == ("nm", "VARCHAR")


# --- middleman (needs duckdb) ----------------------------------------------

_SIMPLE = "-- fixture: simple_table\n-- keys: [id]\nCREATE TABLE simple_table (id INTEGER);\nINSERT INTO simple_table VALUES (1),(2),(3);\n"
_IDNAME = "-- fixture: id_name\nCREATE TABLE id_name (id INTEGER, name VARCHAR);\nINSERT INTO id_name VALUES (1,'a'),(2,'b');\n"


@needs_duckdb
def test_canonicalize_simple(tmp_path):
    fx = parse_fixture(_SIMPLE, str(tmp_path / "simple_table.sql"))
    t = canonicalize(_cli(), fx, workdir=str(tmp_path))
    assert t.name == "simple_table"
    assert t.column_names() == ["id"]
    assert t.columns[0].type == "INTEGER"
    assert t.keys == ["id"]
    assert t.seed_data == [(1,), (2,), (3,)]
    assert t.fixture is fx


@needs_duckdb
def test_instantiator_resolves_types(tmp_path):
    fx = parse_fixture(_IDNAME, str(tmp_path / "id_name.sql"))
    db = str(tmp_path / "out.duckdb")
    t = DuckDBInstantiator().instantiate(fx, db, duckdb_bin=_cli())
    assert [(c.name, c.type) for c in t.columns] == [("id", "INTEGER"), ("name", "VARCHAR")]
    assert t.seed_data == [(1, "a"), (2, "b")]
    assert os.path.isfile(db)  # the db file is the isolation boundary


def test_loading_is_deferred_to_test_run(pytester):
    """A skipped test must NOT read its fixture; a running one resolves at setup.

    Proven without duckdb: `resources` loads from an EMPTY search path, so any
    resolution raises FileNotFoundError. A skipped test that stays green proves the
    load never happened; the running test errors at setup, proving load is deferred
    to run time (never at collection).
    """
    pytester.makeconftest(
        """
        import pytest
        from ducktest import collect_requirements
        from ducktest.fixtures import load_fixture

        @pytest.fixture
        def resources(request):
            # empty search path -> load_fixture raises, but ONLY when actually called
            return [load_fixture(r.source, []) for r in collect_requirements(request.node)]
        """
    )
    pytester.makepyfile(
        """
        import pytest
        from ducktest import requires, Fixture

        @pytest.mark.skip(reason="lazy: resources must not load for a skipped test")
        @requires(source=Fixture("missing"))
        def test_skipped(resources):
            assert False  # never reached

        @requires(source=Fixture("missing"))
        def test_runs(resources):
            assert False  # not reached — resources errors first (fixture read at setup)
        """
    )
    # NB: don't disable xdist here — the driver plugin declares an xdist-only hook
    # (pytest_configure_node); it's inactive without -n, but must stay registered.
    result = pytester.runpytest()
    # skipped test: no load, stays skipped. running test: load attempted -> setup error.
    result.assert_outcomes(skipped=1, errors=1)


@needs_duckdb
def test_instantiator_seed_none_empties(tmp_path):
    fx = parse_fixture(_SIMPLE, str(tmp_path / "simple_table.sql"))
    db = str(tmp_path / "empty.duckdb")
    t = DuckDBInstantiator().instantiate(fx, db, duckdb_bin=_cli(), seed=None)
    assert t.column_names() == ["id"]
    assert t.seed_data == []  # coupled seed dropped


@needs_duckdb
def test_instantiator_seed_replaces(tmp_path):
    fx = parse_fixture(_SIMPLE, str(tmp_path / "simple_table.sql"))
    db = str(tmp_path / "replaced.duckdb")
    t = DuckDBInstantiator().instantiate(fx, db, duckdb_bin=_cli(), seed=[(9,), (10,)])
    assert t.seed_data == [(9,), (10,)]


@needs_duckdb
def test_load_fixture_from_disk(tmp_path):
    (tmp_path / "id_name.sql").write_text(_IDNAME)
    fx = load_fixture(Fixture("id_name"), [tmp_path])  # .sql suffix optional
    t = canonicalize(_cli(), fx, workdir=str(tmp_path))
    assert t.name == "id_name"
    assert len(t.seed_data) == 2
