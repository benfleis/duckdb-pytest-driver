"""ducktest5's unittest runner against a fake binary: every way a test can end must get its own outcome,
and no test may count as passed without an end event that says so."""

import json
import os

import pytest

from ducktest5 import sqllogic
from ducktest5.sqllogic import ERROR, FAILED, PASSED, SKIPPED, Batch

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")


def run(tmp_path, names, **kwargs):
    return sqllogic.run_batch(FAKE, Batch(str(tmp_path / "b001"), names, **kwargs), str(tmp_path))


def test_a_batch_is_two_files(tmp_path):
    run(tmp_path, ["a.test", "b.test"], env={"CATALOG": "duck"}, settings={"threads": 2}, init_sql="SELECT 1")
    batch = tmp_path / "b001"
    assert (batch / "tests.txt").read_text() == "a.test\nb.test\n"
    config = json.loads((batch / "config.json").read_text())
    assert config["test_env"] == [{"env_name": "CATALOG", "env_value": "duck"}]
    assert config["settings"] == [{"name": "threads", "value": "2"}]
    assert open(config["init_sqllogic"]).read() == "statement ok\nSELECT 1;\n"
    assert "skip_error_messages" not in config


def test_a_case_with_a_run_provided_service_does_not_forgive_a_connection_error(tmp_path):
    run(tmp_path, ["a.test"], strict_connection_errors=True)
    assert json.loads((tmp_path / "b001" / "config.json").read_text())["skip_error_messages"] == []


def test_each_ending_gets_its_own_outcome(tmp_path):
    results = run(tmp_path, ["a.test", "b_fail.test", "c_skip.test", "d_giveup.test", "e.test"])
    assert {n: r.outcome for n, r in results.items()} == {
        "a.test": PASSED,
        "b_fail.test": FAILED,
        "c_skip.test": SKIPPED,
        "d_giveup.test": SKIPPED,
        "e.test": PASSED,
    }
    assert "Wrong result in query" in results["b_fail.test"].detail
    assert results["c_skip.test"].detail == "require-env NOT_SET"
    assert results["d_giveup.test"].detail == "skip on error_message matching 'HTTP'"


def test_a_failure_in_a_batch_is_run_again_alone_so_its_text_is_its_own(tmp_path):
    results = run(tmp_path, ["a.test", "b_fail.test", "c_fail.test"])
    assert "c_fail" not in results["b_fail.test"].detail
    assert results["b_fail.test"].kept.endswith("b_fail.test")


def test_a_crash_names_the_test_and_the_rest_of_the_batch_still_runs(tmp_path):
    results = run(tmp_path, ["a.test", "b_crash.test", "c.test"])
    assert results["b_crash.test"].outcome == ERROR
    assert "without an end event" in results["b_crash.test"].detail
    assert (results["a.test"].outcome, results["c.test"].outcome) == (PASSED, PASSED)


def test_a_hung_test_is_killed_at_its_own_timeout_and_the_rest_still_runs(tmp_path):
    results = run(tmp_path, ["a.test", "b_hang.test", "c_fail.test", "d.test"], timeouts={"b_hang.test": 0.5})
    assert results["b_hang.test"].outcome == ERROR
    assert (
        "still running after 0s" in results["b_hang.test"].detail
        or "still running after 1s" in results["b_hang.test"].detail
    )
    assert [results[n].outcome for n in ("a.test", "c_fail.test", "d.test")] == [PASSED, FAILED, PASSED]


def test_the_check_against_the_binary_uses_one_scope_on_both_sides(tmp_path, monkeypatch):
    (tmp_path / "test").mkdir()
    monkeypatch.setattr(
        sqllogic,
        "list_binary_tests",
        lambda binary, working_dir, scratch: {"test/a.test", "test/gone.test", "other_extension/x.test"},
    )
    problems = sqllogic.check_against_binary(
        ["test/a.test", "test/new.test"], FAKE, str(tmp_path), str(tmp_path / "s"), str(tmp_path / "test")
    )
    assert len(problems) == 2
    assert "test/new.test" in problems[0] and "does not know" in problems[0]
    assert "test/gone.test" in problems[1] and "other_extension" not in problems[1]


def test_a_binary_that_lists_nothing_is_an_error_not_an_empty_set(tmp_path):
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\necho 'Errors occurred during startup!' >&2\nexit 1\n")
    silent.chmod(0o755)
    with pytest.raises(sqllogic.UsageError, match="listed nothing"):
        sqllogic.list_binary_tests(str(silent), str(tmp_path), str(tmp_path / "s"))
