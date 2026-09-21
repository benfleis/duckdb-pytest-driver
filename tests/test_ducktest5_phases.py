"""The phases as separate invocations over one run directory: what each file holds, what a phase
refuses, and that the outcomes are the ones a plain `run` of the same selection gives."""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

from ducktest5 import events, phases
from ducktest5.cli import main

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")

FILES = {
    "test/ducktestconf.py": """
        from ducktest5 import default
        RULES = [default(tags=["all"])]
    """,
    "test/a/x.test": "",
    "test/a/y.test": "",
    "test/b/z_fail.test": "",
    "test/b/body.py": """
        from ducktest5 import test

        @test()
        def passes(ctx):
            pass
    """,
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
    # The fake binary lists other tests than this tree holds, so the check against `unittest -l` is off.
    return ["--duckdb-bin", FAKE, "--unittest-bin", FAKE, "--no-binary-check", *more]


def outcomes(run_dir):
    return {e["id"]: e["outcome"] for e in events.read(str(run_dir)) if e["event"] == events.CASE_END}


def phases_of(run_dir, *, exec_args=(), plan_args=()):
    """scan, plan, provision, exec, cleanup: one invocation each."""
    assert main(["scan", "--run-dir", run_dir]) == 0
    assert main(["plan", "--run-dir", run_dir, *plan_args]) == 0
    assert main(["provision", "--run-dir", run_dir]) == 0
    code = main(["exec", "--run-dir", run_dir, *binaries(), *exec_args])
    assert main(["cleanup", "--run-dir", run_dir]) == 0
    return code


# -----------------------------------------------------------------------------
# One run in phases says what one run in one process says
#


def test_the_phases_one_at_a_time_give_what_one_run_gives(tree, capsys):
    assert main(["run", *binaries(), "--temp-dir-root", "runs"]) == 1
    (whole,) = (tree / "runs").iterdir()
    capsys.readouterr()

    assert phases_of("apart") == 1
    assert outcomes("apart") == outcomes(whole)
    capsys.readouterr()
    assert main(["report", "--run-dir", "apart", "--format", "json"]) == 1
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {e["id"] for e in printed if e["event"] == events.CASE_END} == set(outcomes(whole))


def test_each_phase_writes_its_own_file_and_the_files_say_what_schema_they_are(tree):
    phases_of("apart")
    for name in (phases.TESTS_FILE, phases.PLAN_FILE, phases.PROVISION_FILE, events.EVENTS_FILE):
        assert os.path.isfile(os.path.join("apart", name))
    for name in (phases.TESTS_FILE, phases.PLAN_FILE, phases.PROVISION_FILE):
        assert yaml.safe_load(open(os.path.join("apart", name)))["schema"] == 1


def test_the_plan_names_the_batches_the_cases_and_the_knobs(tree):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--batch-size", "1", "--workers", "2"]) == 0
    doc = yaml.safe_load(open(os.path.join("apart", phases.PLAN_FILE)))
    assert doc["knobs"]["batch_size"] == 1 and doc["knobs"]["workers"] == 2
    assert doc["knobs"]["runner"] == "unittest"
    assert [b["id"] for b in doc["batches"]] == ["b001", "b002", "b003", "b004"]
    assert [b["runner"] for b in doc["batches"]] == ["unittest"] * 3 + ["python"]
    assert sorted(b["cases"][0] for b in doc["batches"]) == [
        "a/x.test::x",
        "a/y.test::y",
        "b/body.py::passes",
        "b/z_fail.test::z_fail",
    ]
    assert doc["batches"][0]["key"]["digest"] == doc["batches"][1]["key"]["digest"]  # one environment


def test_what_crosses_to_another_machine_holds_no_absolute_path(tree):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart"]) == 0
    for name in (phases.TESTS_FILE, phases.PLAN_FILE):
        text = open(os.path.join("apart", name)).read()
        assert str(tree) not in text, f"{name} holds a path of this machine"


# -----------------------------------------------------------------------------
# What a phase refuses
#


def test_a_phase_that_ran_refuses_to_write_again_unless_forced(tree, capsys):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["scan", "--run-dir", "apart"]) == 2
    assert "is already there" in capsys.readouterr().err
    assert main(["scan", "--run-dir", "apart", "--force"]) == 0


def test_a_file_of_a_schema_we_do_not_know_stops_the_phase(tree, capsys):
    assert main(["scan", "--run-dir", "apart"]) == 0
    doc = yaml.safe_load(open(os.path.join("apart", phases.TESTS_FILE)))
    doc["schema"] = 99
    yaml.safe_dump(doc, open(os.path.join("apart", phases.TESTS_FILE), "w"))
    assert main(["plan", "--run-dir", "apart"]) == 2
    said = capsys.readouterr().err
    assert "schema 99" in said and "newer ducktest5" in said


def test_a_phase_that_has_no_input_says_which_phase_to_run_first(tree, capsys):
    os.mkdir("apart")
    assert main(["plan", "--run-dir", "apart"]) == 2
    assert "run `ducktest5 scan --run-dir apart` first" in capsys.readouterr().err


def test_a_case_of_the_plan_that_is_not_in_the_tree_any_more_is_a_usage_error(tree, capsys):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart"]) == 0
    os.remove("test/a/x.test")
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 2
    assert "a/x.test::x" in capsys.readouterr().err


def test_exec_refuses_a_plan_that_already_has_events(tree, capsys):
    phases_of("apart")
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 2
    assert "--resume" in capsys.readouterr().err


# -----------------------------------------------------------------------------
# Resume, and a slice of a plan for each machine
#


def ended(run_dir):
    return [e for e in events.read(str(run_dir)) if e["event"] == events.CASE_END]


def started(run_dir):
    return [e["id"] for e in events.read(str(run_dir)) if e["event"] == events.CASE_START]


def test_resume_runs_only_the_cases_that_have_no_end_yet(tree, capsys):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--batch-size", "1"]) == 0
    assert main(["exec", "--run-dir", "apart", "--batch", "b001", *binaries()]) == 0
    assert started("apart") == ["a/x.test::x"]

    assert main(["exec", "--run-dir", "apart", "--resume", *binaries()]) == 1
    assert started("apart").count("a/x.test::x") == 1  # it had an end, so it did not run again
    assert len(ended("apart")) == 4
    assert outcomes("apart") == {
        "a/x.test::x": "passed",
        "a/y.test::y": "passed",
        "b/z_fail.test::z_fail": "failed",
        "b/body.py::passes": "passed",
    }


def test_a_case_that_was_stopped_half_way_runs_again_and_its_event_says_so(tree):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--batch-size", "1"]) == 0
    with open(os.path.join("apart", events.EVENTS_FILE), "w") as f:
        f.write(json.dumps({"event": events.CASE_START, "run": "apart", "id": "a/x.test::x"}) + "\n")

    assert main(["exec", "--run-dir", "apart", "--resume", *binaries()]) == 1
    (again,) = [e for e in ended("apart") if e["id"] == "a/x.test::x"]
    assert again["outcome"] == "passed"
    assert again["note"] == "the run before this one was stopped while this case was running"


def test_only_failed_runs_what_failed_again_and_the_later_outcome_wins(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_UNITTEST_STATE", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tree / "test/a/flaky2.test").write_text("")

    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--batch-size", "1"]) == 0
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 1
    assert outcomes("apart")["a/flaky2.test::flaky2"] == "failed"

    assert main(["exec", "--run-dir", "apart", "--only-failed", *binaries()]) == 1
    assert started("apart").count("a/flaky2.test::flaky2") == 2
    assert outcomes("apart")["a/flaky2.test::flaky2"] == "passed"  # a later event wins
    assert outcomes("apart")["b/z_fail.test::z_fail"] == "failed"


def test_two_execs_over_one_plan_write_beside_each_other(tree, capsys):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--batch-size", "1"]) == 0
    here = os.path.join("apart", "04-events.here.jsonl")
    there = os.path.join("apart", "04-events.there.jsonl")
    assert main(["exec", "--run-dir", "apart", "--batch", "b001", "--events", here, *binaries()]) == 0
    assert main(["exec", "--run-dir", "apart", "--id", "b/z_fail.test::z_fail", "--events", there, *binaries()]) == 1
    assert not os.path.exists(os.path.join("apart", events.EVENTS_FILE))

    capsys.readouterr()
    assert main(["report", "--run-dir", here, "--run-dir", there, "--format", "json"]) == 1
    merged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {e["id"]: e["outcome"] for e in merged if e["event"] == events.CASE_END} == {
        "a/x.test::x": "passed",
        "b/z_fail.test::z_fail": "failed",
    }
    # the run directory is the run: a report of it reads every slice written beside the other
    assert outcomes("apart") == {"a/x.test::x": "passed", "b/z_fail.test::z_fail": "failed"}


def test_a_batch_the_plan_does_not_have_is_a_usage_error(tree, capsys):
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart"]) == 0
    assert main(["exec", "--run-dir", "apart", "--batch", "b404", *binaries()]) == 2
    assert "no batch b404" in capsys.readouterr().err


# -----------------------------------------------------------------------------
# A run that is stopped from outside
#


def still_running(mark):
    """The processes whose command line names `mark`, by reading /proc, not by a pattern that would
    also match the command that asks."""
    out = []
    for pid in (n for n in os.listdir("/proc") if n.isdigit()):
        if int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if mark.encode() in f.read():
                    out.append(int(pid))
        except OSError:
            continue
    return out


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="the check for leftovers reads /proc")
def test_a_run_that_is_stopped_leaves_nothing_running_and_can_be_taken_up_again(tree, tmp_path):
    (tree / "test/a/hang.test").write_text("")
    (tree / "test/ducktestconf.py").write_text(
        textwrap.dedent("""
            from ducktest5 import match
            RULES = [match("a/hang.test", timeout=2)]
        """)
    )
    assert main(["scan", "--run-dir", "apart"]) == 0
    assert main(["plan", "--run-dir", "apart", "--batch-size", "1", "--workers", "4"]) == 0
    started = subprocess.Popen(
        [sys.executable, "-m", "ducktest5.cli", "exec", "--run-dir", "apart", *binaries()],
        cwd=str(tree),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):  # until the batches are under way
        if os.path.isfile(os.path.join("apart", events.EVENTS_FILE)) and ended("apart"):
            break
        time.sleep(0.1)

    started.terminate()
    assert started.wait(timeout=30) == 130
    assert still_running(str(tmp_path / "apart")) == []
    assert "a/hang.test::hang" not in outcomes("apart")  # it was running when the run was stopped

    assert main(["exec", "--run-dir", "apart", "--resume", *binaries()]) == 1
    assert len(outcomes("apart")) == len(phases.ids_of_plan(phases.read_plan("apart")))
    assert outcomes("apart")["a/hang.test::hang"] == "error"  # it is still what it is: it never ends
