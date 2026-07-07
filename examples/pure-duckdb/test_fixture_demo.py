"""Dummy `@requires` + tests on top of the table-fixture scaffolding (pure duckdb).

Each test DECLARES the table it needs as a `Fixture(...)` ref; the `resources` fixture
(conftest.py) instantiates it through the registered instantiator and hands back the
resulting table (schema + seed data). The `properties` knobs are declared but inert
here — pure duckdb has no 2x2; a Databricks/Iceberg instantiator is where they'd bite.
"""

from duckdb_pytest_driver import Fixture, requires


@requires(source=Fixture("simple_table"), access="rw", properties={"commit": "cmt", "storage": "managed"})
def test_simple_table_roundtrips(resources):
    t = resources.tables["simple_table"]
    assert t.column_names() == ["id"]
    assert t.columns[0].type == "INTEGER"
    assert t.seed_data == [(1,), (2,), (3,), (4,), (5,)]


@requires(source=Fixture("id_name"), access="ro")
def test_id_name_types_and_data(resources):
    t = resources.tables["id_name"]
    assert [(c.name, c.type) for c in t.columns] == [("id", "INTEGER"), ("name", "VARCHAR")]
    assert t.keys == ["id"]
    assert t.seed_data[0] == (1, "alice")
    assert len(t.seed_data) == 3


@requires(source=Fixture("simple_table"), access="ro")
@requires(source=Fixture("id_name"), access="ro")
def test_two_fixtures_in_one_db(resources):
    # Stacked @requires -> both fixtures land in the same duckdb db.
    assert set(resources.tables) == {"simple_table", "id_name"}


@requires(source=Fixture("simple_table").Seed(None), access="rw")
def test_seed_none_gives_empty_table(resources):
    # .Seed(None) drops the fixture's coupled seed -> schema only.
    t = resources.tables["simple_table"]
    assert t.column_names() == ["id"]
    assert t.seed_data == []


@requires(source=Fixture("simple_table").Seed([(7,), (8,)]), access="rw")
def test_seed_replaces_rows(resources):
    # .Seed(rows) replaces the fixture's seed with the given rows.
    assert resources.tables["simple_table"].seed_data == [(7,), (8,)]
