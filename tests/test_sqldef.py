"""Tests for the generic SQL-def core (ducktest.sqldef)."""

from ducktest import split_statements, run_sql_file


def test_split_statements_preserves_semicolons_in_strings():
    sql = "INSERT INTO t VALUES ('a;b'); SELECT 'it''s;fine'; DROP TABLE t"
    stmts = split_statements(sql)
    assert len(stmts) == 3
    assert stmts[0] == "INSERT INTO t VALUES ('a;b')"
    assert stmts[1] == "SELECT 'it''s;fine'"
    assert stmts[2] == "DROP TABLE t"


def test_run_sql_file_subs_comments_and_dry_run(tmp_path):
    f = tmp_path / "def.sql"
    f.write_text("-- a leading comment\nCREATE TABLE {table_name} (id INT);\nINSERT INTO {table_name} VALUES (1)\n")

    calls = []
    stmts = run_sql_file(str(f), calls.append, subs={"table_name": "c.s.t"})
    assert stmts == ["CREATE TABLE c.s.t (id INT)", "INSERT INTO c.s.t VALUES (1)"]
    assert calls == stmts  # executed
    assert all("--" not in s for s in stmts)  # comment stripped
    assert all("{table_name}" not in s for s in stmts)  # subs applied

    calls_dry = []
    stmts_dry = run_sql_file(str(f), calls_dry.append, subs={"table_name": "c.s.t"}, dry_run=True)
    assert stmts_dry == stmts  # same statement list
    assert calls_dry == []  # nothing executed
