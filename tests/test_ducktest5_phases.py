"""The phases as separate invocations over one run directory: what each file holds, what a phase
refuses, and that the outcomes are the ones a plain `run` of the same selection gives."""

import json
import os
import textwrap

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
