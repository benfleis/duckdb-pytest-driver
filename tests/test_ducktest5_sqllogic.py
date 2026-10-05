"""ducktest5's unittest runner against a fake binary: every way a test can end must get its own outcome,
and no test may count as passed without an end event that says so."""

import json
import os

import pytest

from ducktest5 import sqllogic
from ducktest5.sqllogic import ERROR, FAILED, PASSED, SKIPPED, Batch
from ducktest5.testenv import Entry, TestEnv

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")


def run(tmp_path, names, **kwargs):
    return sqllogic.run_batch(FAKE, Batch(str(tmp_path / "b001"), names, **kwargs), str(tmp_path))


def test_a_batch_is_two_files(tmp_path):
    test_env = TestEnv(
        values={"catalog": Entry("duck", "@test", name="catalog")},
        init_sql=[Entry("SELECT 1;", "@test", kind="sql", shown="")],
    )
    run(tmp_path, ["a.test", "b.test"], test_env=test_env)
    batch = tmp_path / "b001"
    assert (batch / "tests.txt").read_text() == "a.test\nb.test\n"
    config = json.loads((batch / "config.json").read_text())
    assert config["test_env"] == [{"env_name": "CATALOG", "env_value": "duck"}]
    assert open(config["init_sqllogic"]).read() == "statement ok\nSELECT 1;\n"
    assert os.stat(config["init_sqllogic"]).st_mode & 0o777 == 0o600  # it may hold a secret


def test_the_binary_is_told_where_the_tests_are(tmp_path):
    args = sqllogic.batch_args("unittest", Batch(str(tmp_path / "b001"), ["a.test"]), str(tmp_path / "tree"))
    assert args[1:3] == ["--test-dir", str(tmp_path / "tree")]


def test_no_batch_lets_the_binary_forgive_a_connection_error(tmp_path):
    run(tmp_path, ["a.test"])
    assert json.loads((tmp_path / "b001" / "config.json").read_text())["skip_error_messages"] == []


@pytest.fixture
def flaky_state(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("FAKE_UNITTEST_STATE", str(tmp_path / "state"))


def test_a_test_with_retries_that_passes_on_a_later_attempt_is_passed_and_says_so(tmp_path, flaky_state):
    results = run(tmp_path, ["a.test", "b_flaky3.test"], retries={"b_flaky3.test": 2})
    flaky = results["b_flaky3.test"]
    assert (flaky.outcome, flaky.attempts, flaky.note) == (PASSED, 3, "passed on attempt 3 of 3")
    assert (results["a.test"].outcome, results["a.test"].attempts) == (PASSED, 1)


def test_without_retries_a_failure_is_not_run_again(tmp_path, flaky_state):
    results = run(tmp_path, ["b_flaky2.test"])
    assert (results["b_flaky2.test"].outcome, results["b_flaky2.test"].attempts) == (FAILED, 1)
    assert (tmp_path / "state" / "b_flaky2.test.runs").read_text() == "1"


def test_when_no_attempt_passes_the_first_failure_stays(tmp_path, flaky_state):
    results = run(tmp_path, ["b_flaky9.test"], retries={"b_flaky9.test": 2})
    flaky = results["b_flaky9.test"]
    assert (flaky.outcome, flaky.attempts) == (FAILED, 3)
    assert flaky.kept.startswith(str(tmp_path / "b001" / "tmp"))  # of the first attempt, not of a later one
    assert (tmp_path / "state" / "b_flaky9.test.runs").read_text() == "3"


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


def test_a_test_that_fails_only_beside_others_is_failed_and_the_batch_output_stays(tmp_path):
    results = run(tmp_path, ["a.test", "b_onlyinbatch_fail.test"])
    assert results["b_onlyinbatch_fail.test"].outcome == FAILED
    assert "passed when run alone" in results["b_onlyinbatch_fail.test"].detail
    assert (tmp_path / "b001" / "output.txt").exists()


def test_a_hang_after_the_last_test_blames_no_test_and_nothing_counts_as_passed(tmp_path):
    results = run(tmp_path, ["a.test", "b_stallatexit.test"], timeout_s=0.5)
    assert {r.outcome for r in results.values()} == {ERROR}
    assert "while no test was running" in results["a.test"].detail


def test_an_exit_code_that_no_test_explains_is_an_error(tmp_path):
    results = run(tmp_path, ["a.test", "b_badexit.test"])
    assert {r.outcome for r in results.values()} == {ERROR}
    assert "exit code 7" in results["a.test"].detail


def test_results_timeouts_and_retries_go_by_the_callers_key_not_by_file_name(tmp_path, flaky_state):
    tests = [("test 1", "a.test"), ("test 2", "b_flaky2.test"), ("test 3", "c_hang.test")]
    results = run(tmp_path, tests, retries={"test 2": 1}, timeouts={"test 3": 0.5})
    assert {key: (r.name, r.outcome) for key, r in results.items()} == {
        "test 1": ("a.test", PASSED),
        "test 2": ("b_flaky2.test", PASSED),
        "test 3": ("c_hang.test", ERROR),
    }


def test_a_file_twice_in_one_batch_is_refused(tmp_path):
    with pytest.raises(ValueError, match="once"):
        run(tmp_path, [("t[k=a]", "t.test"), ("t[k=b]", "t.test")])


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


def test_each_test_of_a_batch_has_its_own_seconds(tmp_path):
    results = run(tmp_path, ["a.test", "b_nap.test", "c_fail.test", "d_crash.test"])
    assert 0 <= results["a.test"].seconds < 0.3 <= results["b_nap.test"].seconds < 2
    assert results["c_fail.test"].seconds is not None
    assert results["d_crash.test"].seconds is not None  # no end event: what its run alone took
