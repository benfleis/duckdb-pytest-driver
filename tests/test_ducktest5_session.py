"""ducktest5's DuckDB session: one shell for the run, a fresh instance per test, and the reset levels."""

import os
import shutil

import pytest

from ducktest5.client import SqlError
from ducktest5.engines.duckdb import DuckDBPerCall, DuckDBSession


def _shell():
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


pytestmark = pytest.mark.skipif(_shell() is None, reason="no duckdb shell (set $DUCKDB_SHELL or PATH)")


@pytest.fixture
def session():
    s = DuckDBSession(_shell())
    s.begin_test()
    yield s
    s.close()


def _tables(session):
    return [r[0] for r in session.run(["SELECT table_name FROM duckdb_tables() ORDER BY 1"])[1]]


def test_state_carries_over_between_calls(session):
    session.run(["CREATE TEMP TABLE tmp AS SELECT 7 AS i"])
    assert session.run(["SELECT i FROM tmp"]) == (["i"], [(7,)])


def test_the_next_test_starts_on_a_fresh_instance_in_the_same_process(session):
    session.run(["CREATE TABLE x (i INT)", "SET threads = 2"])
    process = session._process
    session.begin_test()
    assert _tables(session) == []
    assert session.run(["SELECT current_setting('threads') <> 2 AS reset"])[1] == [(True,)]
    assert session._process is process


def test_restart_is_a_new_instance_and_redoes_the_preambles(session):
    session.add_preamble("CREATE TABLE from_preamble (i INT);")
    session.run(["CREATE TABLE made_in_body (i INT)"])
    process = session._process
    session.restart()
    assert _tables(session) == ["from_preamble"]
    assert session._process is process


def test_respawn_is_a_new_process_and_redoes_the_preambles(session):
    session.add_preamble("CREATE TABLE from_preamble (i INT);")
    process = session._process
    session.respawn()
    assert _tables(session) == ["from_preamble"]
    assert session._process is not process


def test_a_failure_stops_the_block_names_the_statement_and_the_shell_lives(session):
    with pytest.raises(SqlError) as e:
        session.run(["CREATE TABLE x (i INT)", "SELECT nope", "CREATE TABLE never (i INT)"])
    assert e.value.statement == 1 and "nope" in str(e.value)
    assert _tables(session) == ["x"]


def test_a_shell_that_died_is_replaced_and_gets_the_preambles_back(session):
    session.add_preamble("CREATE TABLE from_preamble (i INT);")
    session._process.kill()
    session._process.wait()
    assert _tables(session) == ["from_preamble"]


def test_per_call_is_a_cold_process_each_time():
    shell = DuckDBPerCall(_shell())
    shell.run(["CREATE TABLE gone (i INT)"])
    with pytest.raises(SqlError, match="does not exist"):
        shell.run(["SELECT * FROM gone"])


def test_two_columns_of_one_name_stay_two_columns(session):
    assert session.run(["SELECT 1 AS a, 2 AS a"]) == (["a", "a"], [(1, 2)])


def test_a_struct_inside_a_value_is_a_dict(session):
    assert session.run(["SELECT {'a': 1, 'b': [{'c': 2}]} AS s"])[1] == [({"a": 1, "b": [{"c": 2}]},)]


def test_a_trailing_comment_does_not_swallow_the_end_of_the_statement(session):
    assert session.run(["SELECT 2 AS two -- the last word"])[1] == [(2,)]


def test_a_statement_that_prints_text_is_a_result_not_an_error(session):
    columns, rows = session.run(["EXPLAIN SELECT 1"])
    assert columns == ["output"] and "PROJECTION" in rows[0][0].upper()
    with pytest.raises(SqlError, match="Binder Error"):
        session.run(["SELECT nope"])


# -----------------------------------------------------------------------------
# A private HOME
#

_PERSIST = "CREATE PERSISTENT SECRET ducktest_home_probe (TYPE s3, KEY_ID 'k', SECRET 'x')"


@pytest.mark.parametrize("client", [DuckDBSession, DuckDBPerCall])
def test_what_a_test_persists_lands_under_the_private_home_not_the_users(tmp_path, monkeypatch, client):
    users_home = tmp_path / "users-home"
    users_home.mkdir()
    monkeypatch.setenv("HOME", str(users_home))
    c = client(_shell(), home=str(tmp_path / "private"))
    try:
        c.run([_PERSIST])
    finally:
        getattr(c, "close", lambda: None)()
    assert (tmp_path / "private" / ".duckdb" / "stored_secrets" / "ducktest_home_probe.duckdb_secret").exists()
    assert list(users_home.iterdir()) == []


def test_with_no_home_given_a_temporary_one_is_used(tmp_path, monkeypatch):
    users_home = tmp_path / "users-home"
    users_home.mkdir()
    monkeypatch.setenv("HOME", str(users_home))
    DuckDBPerCall(_shell()).run([_PERSIST])
    assert list(users_home.iterdir()) == []
