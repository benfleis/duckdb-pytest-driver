"""`--runner run`: DuckDB's own parallel wrapper as the runner, against a fake wrapper that
forwards a failing batch only, as the real one does."""

import os
import textwrap

import pytest
import yaml

from ducktest5 import events, phases
from ducktest5.cli import main

FAKES = os.path.join(os.path.dirname(__file__), "ducktest5_fakes")
FAKE, WRAPPER = os.path.join(FAKES, "fake_unittest.py"), os.path.join(FAKES, "fake_run.py")

FILES = {
    "test/ducktestconf.py": "RULES = []\n",
    "test/a/x.test": "",
    "test/a/y.test": "",
    "test/b/z_fail.test": "",
    "test/b/c_skip.test": "",
}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    for name, body in FILES.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def binaries(*more):
    return ["--duckdb-bin", FAKE, "--unittest-bin", FAKE, "--run-bin", WRAPPER, "--no-binary-check", *more]


def cases_of(run_dir="runs"):
    return {e["id"]: e for e in events.read(str(run_dir)) if e["event"] == events.CASE_END}


def go(*more, run_dir="one"):
    return main(["run", *binaries(), "--temp-dir-root", run_dir, "--runner", "run", *more])


def the_run(run_dir="one"):
    (made,) = os.listdir(run_dir)
    return os.path.join(run_dir, made)


# -----------------------------------------------------------------------------
# The wrapper runs the batches and every case gets its own outcome
#


def test_the_run_wrapper_gives_every_case_an_outcome_of_its_own(tree):
    assert go() == 1
    cases = cases_of(the_run())
    assert {i.split("::")[-1]: c["outcome"] for i, c in cases.items()} == {
        "x": "passed",
        "y": "passed",
        "z_fail": "failed",
        "c_skip": "skipped",
    }
    assert all(c["runner"] == "run" for c in cases.values())
    assert all(c["seconds"] >= 0 for c in cases.values())  # from the binary's own progress line
    # the output of the process that ran it is what says so, not the wrapper's summary
    assert {c["evidence"] for c in cases.values()} == {"event"}
    assert "Wrong result in query" in cases["b/z_fail.test::z_fail"]["problems"][0]


def test_one_call_of_the_wrapper_for_each_case_environment(tree):
    (tree / "test/ducktestconf.py").write_text(
        textwrap.dedent("""
            from ducktest5 import match
            RULES = [match("b/**", settings={"threads": 2})]
        """)
    )
    assert go("--keep") == 1  # else the call whose tests all passed is removed again
    run_dir = the_run()
    assert sorted(os.listdir(os.path.join(run_dir, "run"))) == ["g001", "g002"]
    lists = {g: open(os.path.join(run_dir, "run", g, "tests.txt")).read().split() for g in ("g001", "g002")}
    assert lists["g001"] == ["test/a/x.test", "test/a/y.test"]
    assert lists["g002"] == ["test/b/c_skip.test", "test/b/z_fail.test"]


def test_the_wrapper_gets_the_flags_it_is_given_and_its_own_options(tree):
    assert go("--keep", "--run-flags", "--batch-size 1 --retry 1", "--workers", "3") == 1
    run_dir = the_run()
    # one process file per batch, so --batch-size 1 made one process per test, and the retry a second
    kept = os.listdir(os.path.join(run_dir, "run", "g001", "processes"))
    assert len(kept) == 4 + 1  # four tests, and the failed one ran again
    plan = yaml.safe_load(open(os.path.join(run_dir, phases.PLAN_FILE)))
    assert plan["knobs"]["runner"] == "run" and plan["knobs"]["run_flags"] == "--batch-size 1 --retry 1"


def test_a_test_the_wrapper_never_reported_falls_back_to_its_summary(tree, capsys, monkeypatch):
    # No process output at all: the shim is the only place events come from, so the summary decides.
    monkeypatch.setattr("ducktest5.runners._kept_output", lambda group_dir: [])
    assert go() == 1
    cases = cases_of(the_run())
    assert {c["evidence"] for c in cases.values()} == {"run-summary"}
    assert [c["outcome"] for c in cases.values()] == ["error"] * 4  # the wrapper did not end green
    assert "no event of their own" in capsys.readouterr().out


def test_the_report_says_that_an_outcome_from_the_summary_is_weaker(tree, capsys, monkeypatch):
    monkeypatch.setattr("ducktest5.runners._kept_output", lambda group_dir: [])
    os.remove("test/b/z_fail.test")
    assert go() == 0  # every test passed, says the wrapper
    cases = cases_of(the_run())
    assert {c["outcome"] for c in cases.values()} == {"passed"}
    assert all("seconds" not in c or c["seconds"] is None for c in cases.values())
    said = capsys.readouterr().out
    assert "3 of 3 cases have no event of their own" in said
    capsys.readouterr()
    assert main(["report", "--run-dir", the_run(), "--format", "text", "--level", "terse"]) == 0
    assert "; from-run-summary=3" in capsys.readouterr().out


def test_a_python_body_runs_here_whatever_the_runner_says(tree):
    (tree / "test/b/body.py").write_text(
        textwrap.dedent("""
            from ducktest5 import test

            @test()
            def passes(ctx):
                pass
        """)
    )
    assert go() == 1
    cases = cases_of(the_run())
    assert cases["b/body.py::passes"]["runner"] == "python"


def test_a_missing_wrapper_is_a_usage_error_that_names_the_other_runner(tree, capsys):
    assert (
        main(
            [
                "run",
                "--duckdb-bin",
                FAKE,
                "--unittest-bin",
                FAKE,
                "--no-binary-check",
                "--temp-dir-root",
                "one",
                "--runner",
                "run",
            ]
        )
        == 2
    )
    assert "--runner unittest" in capsys.readouterr().err


def test_the_wrapper_is_given_paths_that_do_not_depend_on_where_it_runs(tree):
    # It runs from the DuckDB source root, so a run directory given relative to the caller is not its own.
    assert main(["scan", "test/a", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--runner", "run"]) == 0
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert {c["outcome"] for c in cases_of("apart").values()} == {"passed"}
    assert {c["evidence"] for c in cases_of("apart").values()} == {"event"}
