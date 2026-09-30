"""ducktest5's DuckDB shell: one process for the run, a fresh instance per test, and the reset levels."""

import os
import shutil

import pytest

from ducktest5.resources.client import SqlError
from ducktest5.engines.duckdb import DuckDBShell


def _shell():
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


pytestmark = pytest.mark.skipif(_shell() is None, reason="no duckdb shell (set $DUCKDB_SHELL or PATH)")


@pytest.fixture
def shell():
    s = DuckDBShell(_shell())
    s.begin_test()
    yield s
    s.close()


def _tables(shell):
    return [r[0] for r in shell.run(["SELECT table_name FROM duckdb_tables() ORDER BY 1"])[1]]


def test_state_carries_over_between_calls(shell):
    shell.run(["CREATE TEMP TABLE tmp AS SELECT 7 AS i"])
    assert shell.run(["SELECT i FROM tmp"]) == (["i"], [(7,)])


def test_the_next_test_starts_on_a_fresh_instance_in_the_same_process(shell):
    shell.run(["CREATE TABLE x (i INT)", "SET threads = 2"])
    process = shell._process
    shell.begin_test()
    assert _tables(shell) == []
    assert shell.run(["SELECT current_setting('threads') <> 2 AS reset"])[1] == [(True,)]
    assert shell._process is process


def test_restart_is_a_new_instance_and_redoes_the_preambles(shell):
    shell.add_preamble("CREATE TABLE from_preamble (i INT);")
    shell.run(["CREATE TABLE made_in_body (i INT)"])
    process = shell._process
    shell.restart()
    assert _tables(shell) == ["from_preamble"]
    assert shell._process is process


def test_respawn_is_a_new_process_and_redoes_the_preambles(shell):
    shell.add_preamble("CREATE TABLE from_preamble (i INT);")
    process = shell._process
    shell.respawn()
    assert _tables(shell) == ["from_preamble"]
    assert shell._process is not process


def test_a_failure_stops_the_block_names_the_statement_and_the_shell_lives(shell):
    with pytest.raises(SqlError) as e:
        shell.run(["CREATE TABLE x (i INT)", "SELECT nope", "CREATE TABLE never (i INT)"])
    assert e.value.statement == 1 and "nope" in str(e.value)
    assert _tables(shell) == ["x"]


def test_a_shell_that_died_is_replaced_and_gets_the_preambles_back(shell):
    shell.add_preamble("CREATE TABLE from_preamble (i INT);")
    shell._process.kill()
    shell._process.wait()
    assert _tables(shell) == ["from_preamble"]


def test_two_columns_of_one_name_stay_two_columns(shell):
    assert shell.run(["SELECT 1 AS a, 2 AS a"]) == (["a", "a"], [(1, 2)])


def test_a_struct_inside_a_value_is_a_dict(shell):
    assert shell.run(["SELECT {'a': 1, 'b': [{'c': 2}]} AS s"])[1] == [({"a": 1, "b": [{"c": 2}]},)]


def test_a_trailing_comment_does_not_swallow_the_end_of_the_statement(shell):
    assert shell.run(["SELECT 2 AS two -- the last word"])[1] == [(2,)]


def test_a_statement_that_prints_text_is_a_result_not_an_error(shell):
    columns, rows = shell.run(["EXPLAIN SELECT 1"])
    assert columns == ["output"] and "PROJECTION" in rows[0][0].upper()
    with pytest.raises(SqlError, match="Binder Error"):
        shell.run(["SELECT nope"])


# -----------------------------------------------------------------------------
# A private HOME
#

_PERSIST = "CREATE PERSISTENT SECRET ducktest_home_probe (TYPE s3, KEY_ID 'k', SECRET 'x')"


def test_what_a_test_persists_lands_under_the_private_home_not_the_users(tmp_path, monkeypatch):
    users_home = tmp_path / "users-home"
    users_home.mkdir()
    monkeypatch.setenv("HOME", str(users_home))
    c = DuckDBShell(_shell(), home=str(tmp_path / "private"))
    try:
        c.run([_PERSIST])
    finally:
        c.close()
    assert (tmp_path / "private" / ".duckdb" / "stored_secrets" / "ducktest_home_probe.duckdb_secret").exists()
    assert list(users_home.iterdir()) == []


def test_with_no_home_given_a_temporary_one_is_used(tmp_path, monkeypatch):
    users_home = tmp_path / "users-home"
    users_home.mkdir()
    monkeypatch.setenv("HOME", str(users_home))
    c = DuckDBShell(_shell())
    try:
        c.run([_PERSIST])
    finally:
        c.close()
    assert list(users_home.iterdir()) == []
