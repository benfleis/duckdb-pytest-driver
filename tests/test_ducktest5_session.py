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
