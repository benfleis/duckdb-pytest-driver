"""Several workers: the same outcomes as one, tests that overlap, and tests that must not."""

import os
import re
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from ducktest5 import events, phases, sqllogic
from ducktest5.engines.duckdb import DuckDBShell
from test_ducktest5_run import FAKE, FakeShell, go, outcomes, py_test, sql_test

SLEEP_S = 0.3  # long enough that tests overlap, short enough for a self-test
FAKE_SHELL = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_shell.py")


class Recorder:
    """What each body did and when, for a test that asks whether two tests overlapped."""

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
        """Whether the two tests of these names were in their bodies at the same time."""
        (_, a_in, a_out) = next(s for s in self.spans if s[0] == one)
        (_, b_in, b_out) = next(s for s in self.spans if s[0] == other)
        return a_in < b_out and b_in < a_out


def py_tests_of(recorder, names, *, seconds=SLEEP_S, **fields):
    return [py_test(recorder.body(n, seconds=seconds), path=f"test/{n}.py", **fields) for n in names]


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
        tests = [
            *py_tests_of(recorder, [n for n in names if not n.endswith("_fails")], seconds=0.05),
            py_test(recorder.body("e_fails", seconds=0.05, raises=True), path="test/e_fails.py"),
            sql_test("a.test"),
            sql_test("b_fail.test"),
        ]
        code, _, report, _ = go(tree_for(tmp_path, f"w{workers}"), tests, workers=workers)
        return code, outcomes(report)

    assert run(1) == run(4)


def test_tests_run_at_the_same_time_and_one_worker_runs_them_one_after_another(tmp_path):
    def wall(workers):
        recorder = Recorder()
        started = time.perf_counter()
        code, _, _, _ = go(
            tree_for(tmp_path, f"w{workers}"), py_tests_of(recorder, ["a", "b", "c", "d"]), workers=workers
        )
        assert code == 0
        return time.perf_counter() - started, recorder

    alone, one = wall(1)
    together, four = wall(4)
    assert not one.overlap("a", "b")
    assert four.overlap("a", "b")
    assert together < alone / 2


def test_a_test_that_raises_does_not_stop_the_others(tmp_path):
    recorder = Recorder()
    tests = [
        *py_tests_of(recorder, ["a"], seconds=0.05),
        py_test(recorder.body("b_raises", seconds=0.05, raises=True), path="test/b_raises.py"),
        *py_tests_of(recorder, ["c"], seconds=0.05),
    ]
    code, _, report, _ = go(tmp_path, tests, workers=4)
    assert code == 1
    assert outcomes(report) == {"a": "passed", "b_raises": "failed", "c": "passed"}


def test_every_worker_has_a_shell_of_its_own_and_keeps_it_warm(tmp_path):
    made = []

    class Counted(FakeShell):
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

    tests = [py_test(uses_the_shell(n), path=f"test/{n}.py") for n in ("a", "b", "c", "d")]
    assert go(tmp_path, tests, workers=2, shell_factory=Counted)[0] == 0
    assert len(made) == 2  # one for each worker, not one for each test
    assert sum(len(s.statements) for s in made) == 4


def test_a_test_that_changes_the_environment_still_gets_a_shell_of_its_own(tmp_path):
    from ducktest5.declare import Variant

    made = []

    class Counted(FakeShell):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    def body(ctx):
        ctx.duckdb().oks("SELECT 1")

    tests = [
        py_test(body, {"auth": Variant("profile", process_env={"AWS_PROFILE": "ducktest"})}, path="test/env.py"),
        py_test(body, path="test/plain.py"),
    ]
    assert go(tmp_path, tests, workers=2, shell_factory=Counted)[0] == 0
    with_env = [s for s in made if s.env is not None]
    assert len(with_env) == 1 and with_env[0].env["AWS_PROFILE"] == "ducktest"


# -----------------------------------------------------------------------------
# A mutex: what must never overlap
#


def test_two_tests_that_name_the_same_mutex_never_overlap(tmp_path):
    recorder = Recorder()
    tests = [
        *py_tests_of(recorder, ["a", "b"], mutex=["the-host-port"]),
        *py_tests_of(recorder, ["c"]),
    ]
    assert go(tmp_path, tests, workers=4)[0] == 0
    assert not recorder.overlap("a", "b")
    assert recorder.overlap("a", "c") or recorder.overlap("b", "c")  # the mutex holds back nothing else


def test_a_test_takes_its_mutexes_in_sorted_order_whatever_order_it_names_them(tmp_path):
    recorder = Recorder()
    tests = [
        *py_tests_of(recorder, ["a"], mutex=["one", "two"]),
        *py_tests_of(recorder, ["b"], mutex=["two", "one"]),
    ]
    assert go(tmp_path, tests, workers=2)[0] == 0
    assert not recorder.overlap("a", "b")


def test_the_plan_says_which_batches_hold_a_mutex(tmp_path):
    import yaml

    recorder = Recorder()
    tests = [*py_tests_of(recorder, ["a"], mutex=["the-host-port"]), *py_tests_of(recorder, ["b"])]
    _, run_dir, _, _ = go(tmp_path, tests, workers=2)
    plan = yaml.safe_load((run_dir / "02-plan.yaml").read_text())
    assert [b.get("mutex") for b in plan["batches"]] == [["the-host-port"], None]


# -----------------------------------------------------------------------------
# What the reader is told while it runs
#


def test_the_live_lines_come_one_by_one_in_the_order_the_tests_ended(tmp_path):
    recorder = Recorder()
    tests = [*py_tests_of(recorder, ["a", "b", "c", "d"]), sql_test("x.test")]
    _, run_dir, _, text = go(tmp_path, tests, workers=4)
    lines = [line for line in text.splitlines() if re.match(r"^\s*\[\d+/\d+]", line)]
    assert [re.match(r"^\s*\[(\d+)/(\d+)]", line).groups() for line in lines] == [(str(n), "5") for n in range(1, 6)]
    ended = [e["id"] for e in events.read(str(run_dir)) if e["event"] == events.TEST_END]
    assert [line.split()[2] for line in lines] == ended  # one line for each test, in the order they ended


# -----------------------------------------------------------------------------
# A run that is stopped from outside, with workers > 1
#

TREE = {
    "test/ducktest_conf.py": "RULES = []",
    "test/slow.py": """
        import time

        from ducktest5 import test

        @test(params={"a": {}, "b": {}, "c": {}, "d": {}})
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
    for _ in range(300):  # until the first tests are under way
        lines = [e for f in (tmp_path / "runs").glob("*/04-events.jsonl") for e in events.read(str(f))]
        if any(e["event"] == events.TEST_START for e in lines):
            break
        time.sleep(0.1)

    started.terminate()
    assert started.wait(timeout=30) == 130
    assert still_running(str(tmp_path)) == []
    (run_dir,) = (tmp_path / "runs").iterdir()
    ended = {e["id"] for e in events.read(str(run_dir)) if e["event"] == events.TEST_END}
    assert len(ended) < 4  # what was under way has no end: a resume takes exactly those up again


# -----------------------------------------------------------------------------
# A timeout on a Python test
#


def _named(fn, name):
    fn.__name__ = name
    return fn


def _quick_tests(n):
    return [py_test(_named(lambda ctx: None, f"quick{i}"), path=f"test/quick{i}.py") for i in range(n)]


@pytest.mark.parametrize("workers", [1, 4])
def test_a_body_that_sleeps_past_its_timeout_ends_error_and_the_run_moves_on(tmp_path, workers):
    def slow(ctx):
        time.sleep(0.5)

    tests = [py_test(_named(slow, "slow"), path="test/slow.py", timeout=0.1), *_quick_tests(3)]
    t0 = time.perf_counter()
    code, run_dir, report, _ = go(tmp_path, tests, workers=workers)
    took = time.perf_counter() - t0
    out = outcomes(report)
    assert out["slow"] == "error"
    assert all(out[f"quick{i}"] == "passed" for i in range(3))
    note = next(c["note"] for c in report["tests"] if c["id"].endswith("::slow"))
    assert re.match(r"^timed out after \d+s$", note)
    assert took < 3  # the run did not wait out the full sleep


@pytest.mark.parametrize("workers", [1, 4])
def test_a_hung_duckdb_statement_is_released_and_reported_once(tmp_path, workers):
    def hangs(ctx):
        ctx.duckdb().oks("SELECT drip")

    tests = [py_test(_named(hangs, "hangs"), path="test/hangs.py", timeout=0.3), *_quick_tests(3)]
    code, run_dir, report, _ = go(tmp_path, tests, workers=workers, duckdb_bin=FAKE_SHELL, shell_factory=DuckDBShell)
    out = outcomes(report)
    assert out["hangs"] == "error"
    assert all(out[f"quick{i}"] == "passed" for i in range(3))
    (test,) = [c for c in report["tests"] if c["id"].endswith("::hangs")]
    assert re.match(r"^timed out after \d+s$", test["note"])
    ends = [e for e in events.read(str(run_dir)) if e["event"] == events.TEST_END and e["id"] == test["id"]]
    assert len(ends) == 1  # the dead-shell exception the timeout let loose was swallowed, not reported again


def test_a_rules_timeout_reaches_a_python_test_and_it_defaults_like_a_sqllogic_test_does():
    default_test = py_test(_named(lambda ctx: None, "default"), path="test/default.py")
    custom_test = py_test(_named(lambda ctx: None, "custom"), path="test/custom.py", timeout=42)
    plan = phases.build_plan([default_test, custom_test], workers=2)
    by_file = {b["files"][0]: b for b in plan["batches"]}
    assert by_file["test/default.py"]["timeout"] == sqllogic.DEFAULT_TIMEOUT_S
    assert by_file["test/custom.py"]["timeout"] == 42
