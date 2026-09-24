"""Several workers: the same outcomes as one, cases that overlap, and cases that must not."""

import os
import re
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from ducktest5 import events, phases, sqllogic
from ducktest5.engines.duckdb import DuckDBSession
from test_ducktest5_run import FAKE, FakeSession, go, outcomes, py_case, sql_case

SLEEP_S = 0.3  # long enough that cases overlap, short enough for a self-test
FAKE_SHELL = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_shell.py")


class Recorder:
    """What each body did and when, for a test that asks whether two cases overlapped."""

    def __init__(self):
        self.spans = []  # (name, entered, left)
        self._lock = threading.Lock()

    def body(self, name, *, seconds=SLEEP_S, raises=False):
        def run(ctx):
            entered = time.perf_counter()
            time.sleep(seconds)
            with self._lock:
                self.spans.append((name, entered, time.perf_counter()))
            if raises:
                raise RuntimeError(f"{name} broke")

        run.__name__ = name
        return run

    def overlap(self, one, other):
        """Whether the two cases of these names were in their bodies at the same time."""
        (_, a_in, a_out) = next(s for s in self.spans if s[0] == one)
        (_, b_in, b_out) = next(s for s in self.spans if s[0] == other)
        return a_in < b_out and b_in < a_out


def cases_of(recorder, names, *, seconds=SLEEP_S, **fields):
    return [py_case(recorder.body(n, seconds=seconds), path=f"test/{n}.py", **fields) for n in names]


def tree_for(tmp_path, name):
    """A working directory of its own, so that two runs of one test do not share a run dir."""
    (tmp_path / name).mkdir()
    return tmp_path / name


# -----------------------------------------------------------------------------
# What several workers change, and what they do not
#


def test_four_workers_give_the_same_outcomes_as_one(tmp_path):
    names = ["a", "b", "c", "d", "e_fails", "f"]

    def run(workers):
        recorder = Recorder()
        cases = [
            *cases_of(recorder, [n for n in names if not n.endswith("_fails")], seconds=0.05),
            py_case(recorder.body("e_fails", seconds=0.05, raises=True), path="test/e_fails.py"),
            sql_case("a.test"),
            sql_case("b_fail.test"),
        ]
        code, _, report, _ = go(tree_for(tmp_path, f"w{workers}"), cases, workers=workers)
        return code, outcomes(report)

    assert run(1) == run(4)


def test_cases_run_at_the_same_time_and_one_worker_runs_them_one_after_another(tmp_path):
    def wall(workers):
        recorder = Recorder()
        started = time.perf_counter()
        code, _, _, _ = go(tree_for(tmp_path, f"w{workers}"), cases_of(recorder, ["a", "b", "c", "d"]), workers=workers)
        assert code == 0
        return time.perf_counter() - started, recorder

    alone, one = wall(1)
    together, four = wall(4)
    assert not one.overlap("a", "b")
    assert four.overlap("a", "b")
    assert together < alone / 2


def test_a_case_that_raises_does_not_stop_the_others(tmp_path):
    recorder = Recorder()
    cases = [
        *cases_of(recorder, ["a"], seconds=0.05),
        py_case(recorder.body("b_raises", seconds=0.05, raises=True), path="test/b_raises.py"),
        *cases_of(recorder, ["c"], seconds=0.05),
    ]
    code, _, report, _ = go(tmp_path, cases, workers=4)
    assert code == 1
    assert outcomes(report) == {"a": "passed", "b_raises": "failed", "c": "passed"}


def test_every_worker_has_a_shell_of_its_own_and_keeps_it_warm(tmp_path):
    made = []

    class Counted(FakeSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    recorder = Recorder()

    def uses_the_shell(name):
        body = recorder.body(name)

        def run(ctx):
            ctx.duckdb().oks(f"SELECT '{name}'")
            body(ctx)

        run.__name__ = name
        return run

    cases = [py_case(uses_the_shell(n), path=f"test/{n}.py") for n in ("a", "b", "c", "d")]
    assert go(tmp_path, cases, workers=2, session_factory=Counted)[0] == 0
    assert len(made) == 2  # one for each worker, not one for each case
    assert sum(len(s.statements) for s in made) == 4


def test_a_case_that_changes_the_environment_still_gets_a_shell_of_its_own(tmp_path):
    from ducktest5.declare import Variant

    made = []

    class Counted(FakeSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    def body(ctx):
        ctx.duckdb().oks("SELECT 1")

    cases = [
        py_case(body, {"auth": Variant("profile", env={"AWS_PROFILE": "ducktest"})}, path="test/env.py"),
        py_case(body, path="test/plain.py"),
    ]
    assert go(tmp_path, cases, workers=2, session_factory=Counted)[0] == 0
    with_env = [s for s in made if s.env is not None]
    assert len(with_env) == 1 and with_env[0].env["AWS_PROFILE"] == "ducktest"


# -----------------------------------------------------------------------------
# A mutex: what must never overlap
#


def test_two_cases_that_name_the_same_mutex_never_overlap(tmp_path):
    recorder = Recorder()
    cases = [
        *cases_of(recorder, ["a", "b"], mutex=["the-host-port"]),
        *cases_of(recorder, ["c"]),
    ]
    assert go(tmp_path, cases, workers=4)[0] == 0
    assert not recorder.overlap("a", "b")
    assert recorder.overlap("a", "c") or recorder.overlap("b", "c")  # the mutex holds back nothing else


def test_a_case_takes_its_mutexes_in_sorted_order_whatever_order_it_names_them(tmp_path):
    recorder = Recorder()
    cases = [
        *cases_of(recorder, ["a"], mutex=["one", "two"]),
        *cases_of(recorder, ["b"], mutex=["two", "one"]),
    ]
    assert go(tmp_path, cases, workers=2)[0] == 0
    assert not recorder.overlap("a", "b")


def test_the_plan_says_which_batches_hold_a_mutex(tmp_path):
    import yaml

    recorder = Recorder()
    cases = [*cases_of(recorder, ["a"], mutex=["the-host-port"]), *cases_of(recorder, ["b"])]
    _, run_dir, _, _ = go(tmp_path, cases, workers=2)
    plan = yaml.safe_load((run_dir / "02-plan.yaml").read_text())
    assert [b.get("mutex") for b in plan["batches"]] == [["the-host-port"], None]


# -----------------------------------------------------------------------------
# What the reader is told while it runs
#


def test_the_live_lines_come_one_by_one_in_the_order_the_cases_ended(tmp_path):
    recorder = Recorder()
    cases = [*cases_of(recorder, ["a", "b", "c", "d"]), sql_case("x.test")]
    _, run_dir, _, text = go(tmp_path, cases, workers=4)
    lines = [line for line in text.splitlines() if re.match(r"^\s*\[\d+/\d+]", line)]
    assert [re.match(r"^\s*\[(\d+)/(\d+)]", line).groups() for line in lines] == [(str(n), "5") for n in range(1, 6)]
    ended = [e["id"] for e in events.read(str(run_dir)) if e["event"] == events.CASE_END]
    assert [line.split()[2] for line in lines] == ended  # one line for each case, in the order they ended


# -----------------------------------------------------------------------------
# A run that is stopped from outside, with workers > 1
#

TREE = {
    "test/ducktest_conf.py": "RULES = []",
    "test/slow.py": """
        import time

        from ducktest5 import test

        @test(cases={"a": {}, "b": {}, "c": {}, "d": {}})
        def waits(ctx):
            time.sleep(5)
    """,
}


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
def test_a_stopped_run_with_several_workers_ends_by_itself_and_leaves_nothing_running(tmp_path):
    for name, body in TREE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
    started = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ducktest5.cli",
            "run",
            "--duckdb-bin",
            FAKE,
            "--workers",
            "2",
            "--temp-dir-root",
            "runs",
        ],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(300):  # until the first cases are under way
        lines = [e for f in (tmp_path / "runs").glob("*/04-events.jsonl") for e in events.read(str(f))]
        if any(e["event"] == events.CASE_START for e in lines):
            break
        time.sleep(0.1)

    started.terminate()
    assert started.wait(timeout=30) == 130
    assert still_running(str(tmp_path)) == []
    (run_dir,) = (tmp_path / "runs").iterdir()
    ended = {e["id"] for e in events.read(str(run_dir)) if e["event"] == events.CASE_END}
    assert len(ended) < 4  # what was under way has no end: a resume takes exactly those up again


# -----------------------------------------------------------------------------
# A timeout on a Python case
#


def _named(fn, name):
    fn.__name__ = name
    return fn


def _quick_cases(n):
    return [py_case(_named(lambda ctx: None, f"quick{i}"), path=f"test/quick{i}.py") for i in range(n)]


@pytest.mark.parametrize("workers", [1, 4])
def test_a_body_that_sleeps_past_its_timeout_ends_error_and_the_run_moves_on(tmp_path, workers):
    def slow(ctx):
        time.sleep(0.5)

    cases = [py_case(_named(slow, "slow"), path="test/slow.py", timeout=0.1), *_quick_cases(3)]
    t0 = time.perf_counter()
    code, run_dir, report, _ = go(tmp_path, cases, workers=workers)
    took = time.perf_counter() - t0
    out = outcomes(report)
    assert out["slow"] == "error"
    assert all(out[f"quick{i}"] == "passed" for i in range(3))
    note = next(c["note"] for c in report["cases"] if c["id"].endswith("::slow"))
    assert re.match(r"^timed out after \d+s$", note)
    assert took < 3  # the run did not wait out the full sleep


@pytest.mark.parametrize("workers", [1, 4])
def test_a_hung_duckdb_statement_is_released_and_reported_once(tmp_path, workers):
    def hangs(ctx):
        ctx.duckdb().oks("SELECT drip")

    cases = [py_case(_named(hangs, "hangs"), path="test/hangs.py", timeout=0.3), *_quick_cases(3)]
    code, run_dir, report, _ = go(
        tmp_path, cases, workers=workers, duckdb_bin=FAKE_SHELL, session_factory=DuckDBSession
    )
    out = outcomes(report)
    assert out["hangs"] == "error"
    assert all(out[f"quick{i}"] == "passed" for i in range(3))
    (case,) = [c for c in report["cases"] if c["id"].endswith("::hangs")]
    assert re.match(r"^timed out after \d+s$", case["note"])
    ends = [e for e in events.read(str(run_dir)) if e["event"] == events.CASE_END and e["id"] == case["id"]]
    assert len(ends) == 1  # the dead-shell exception the timeout let loose was swallowed, not reported again


def test_a_rules_timeout_reaches_a_python_case_and_it_defaults_like_a_test_case_does():
    default_case = py_case(_named(lambda ctx: None, "default"), path="test/default.py")
    custom_case = py_case(_named(lambda ctx: None, "custom"), path="test/custom.py", timeout=42)
    plan = phases.build_plan([default_case, custom_case], workers=2)
    by_test = {b["tests"][0]: b for b in plan["batches"]}
    assert by_test["test/default.py"]["timeout"] == sqllogic.DEFAULT_TIMEOUT_S
    assert by_test["test/custom.py"]["timeout"] == 42
