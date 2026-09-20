"""The `unittest` runner: `.test` bodies, in batches, through DuckDB's own binary.

A batch is two files in its own directory, and that is the whole contract with the binary:

    tests.txt     one test path per line (the binary's exact `-f` mode: only these are registered)
    config.json   the `--test-config` file: env for `{NAME}` in the body, settings, init SQL, temp root

Every test must end with an event from the binary that says how it ended. A test with no such event
did not run as far as this runner knows, and is never counted as passed.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .binary import UsageError

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"
_EVENT = re.compile(r"\[TEST_EVENT\] (\{.*\})")
_SKIP = re.compile(r"\[SKIP_TEST\] (.*?) :: (.*)")
_PROGRESS = re.compile(r"^\[\d+/\d+\] \(\d+%\): .*$", re.M)
DEFAULT_TIMEOUT_S = 600
_OUTSIDE_A_TEST = object()  # the process hung while no test was running: at startup, between tests, at exit


def find_unittest(*, explicit=None, build="debug", working_dir="."):
    if explicit:
        path = explicit
    elif os.environ.get("BUILD_DIR"):
        path = os.path.join(os.environ["BUILD_DIR"], "test", "unittest")
    else:
        path = os.path.join(working_dir, "build", build, "test", "unittest")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise UsageError(
            f"unittest binary not found at {path}. Build build/<variant>/test/unittest (selected by --build / "
            "$BUILD_DIR), or pass --unittest-bin PATH."
        )
    return path


@dataclass
class TestResult:
    name: str
    outcome: str
    seconds: float | None = None
    detail: str = ""
    kept: str | None = None
    note: str = ""


@dataclass
class Batch:
    dir: str
    names: list
    env: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    init_sql: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S  # for one test, counted from the binary's begin event for it
    strict_connection_errors: bool = False
    timeouts: dict = field(default_factory=dict)  # test name -> seconds, where a test has its own

    def timeout_for(self, name):
        return self.timeouts.get(name) or self.timeout_s

    def write(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "tests.txt"), "w") as f:
            f.write("".join(n + "\n" for n in self.names))
        config = {
            "description": "a ducktest batch",
            "temp_dir_root": os.path.join(self.dir, "tmp"),
            "test_env": [{"env_name": k, "env_value": str(v)} for k, v in sorted(self.env.items())],
            "settings": [{"name": k, "value": str(v)} for k, v in sorted(self.settings.items())],
        }
        if self.strict_connection_errors:
            # By default the binary ends a file early, as not failed, when an error mentions 'HTTP' or
            # 'Unable to connect': a test against the public internet must not fail without a network.
            # When the run itself provides the service, the same error means the service died.
            config["skip_error_messages"] = []
        if self.init_sql:
            init = os.path.join(self.dir, "init.sqllogic")
            with open(init, "w") as f:
                f.write("statement ok\n" + self.init_sql.strip().rstrip(";") + ";\n")
            config["init_sqllogic"] = init
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump(config, f, indent=1)


def _events(output):
    begun, ended = [], {}
    for m in _EVENT.finditer(output):
        try:
            ev = json.loads(m.group(1))
        except ValueError:
            continue
        if ev.get("event") == "begin":
            begun.append(ev.get("name"))
        elif ev.get("event") == "end":
            ended[ev.get("name")] = ev
    return begun, ended


def _skip_reasons(output):
    """{test name: reason} from the binary's skip lines. The end event says `ok` for a file that the
    binary gave up on (an error that matches its skip list, a `mode skip`), so the reason is here only."""
    return {name.strip(): reason.strip() for name, reason in _SKIP.findall(output)}


def _human(output):
    """The binary's own text, without the event lines, the skip lines and the progress lines."""
    text = _EVENT.sub("", output)
    text = _SKIP.sub("", text)
    text = _PROGRESS.sub("", text)
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip()).strip()


def _invoke(binary, batch, working_dir):
    """Run the batch and watch it: a test that is still running after its timeout gets the process
    killed. Returns (output, what was running when it was killed or None, the exit code)."""
    batch.write()
    args = [
        binary,
        "-f",
        os.path.join(batch.dir, "tests.txt"),
        "--test-config",
        os.path.join(batch.dir, "config.json"),
        "--emit-test-events",
        "--emit-on-skip",
    ]
    proc = subprocess.Popen(
        args, cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    lines = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(line) for line in proc.stdout] and None or lines.put(None), daemon=True
    ).start()
    output, running, since, killed_for = [], None, time.perf_counter(), None
    while True:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            line = ""
        if line is None:
            break
        if line:
            output.append(line)
            for m in _EVENT.finditer(line):
                try:
                    ev = json.loads(m.group(1))
                except ValueError:
                    continue
                if ev.get("event") == "begin":
                    running, since = ev.get("name"), time.perf_counter()
                elif ev.get("event") == "end":
                    running, since = None, time.perf_counter()
        # Before the first begin event the binary starts up and loads extensions; that shares the
        # batch's general timeout.
        limit = batch.timeout_for(running) if running else batch.timeout_s
        if time.perf_counter() - since > limit:
            killed_for = running or _OUTSIDE_A_TEST
            proc.kill()
            break
    returncode = proc.wait()
    text = "".join(output)
    with open(os.path.join(batch.dir, "output.txt"), "w") as f:
        f.write(text)
    return text, killed_for, returncode


def _classify(name, ev, detail, skip_reason=None):
    status = ev.get("status")
    if skip_reason and status != "error":
        return TestResult(name, SKIPPED, detail=skip_reason)
    kept = ev.get("temp_dir") if status == "error" else None
    if status == "ok":
        if ev.get("skip-mode", 0):
            return TestResult(name, PASSED, note="part of the file did not run (mode skip)")
        return TestResult(name, PASSED)
    if status == "skip-requirement":
        return TestResult(name, SKIPPED, detail=detail or "a `require` or `require-env` of the file is not met")
    return TestResult(name, FAILED, detail=(ev.get("data") or "") + ("\n" + detail if detail else ""), kept=kept)


def _sub(batch, tag, names):
    return Batch(
        os.path.join(batch.dir, tag),
        names,
        batch.env,
        batch.settings,
        batch.init_sql,
        batch.timeout_s,
        batch.strict_connection_errors,
        batch.timeouts,
    )


def run_batch(binary, batch, working_dir):
    """Run one batch; returns {name: TestResult}, one for every name.

    - An end event is the only thing that gives a test an outcome here. A test that was still running
      at its timeout is an ERROR. A test with no end event runs again: the ones after a killed test as
      a new batch, the others alone; with no end event alone, it is an ERROR.
    - A test that failed inside a batch of several runs again alone for its text only. Its outcome
      stays FAILED also when it passes alone: a failure that needs the other files of the batch is
      state that leaks between tests, which is a failure.
    - When the process hung while no test was running, or ended with an exit code that the end events
      do not explain, every test of the batch that would count as passed is an ERROR instead.
    """
    t0 = time.perf_counter()
    output, killed_for, returncode = _invoke(binary, batch, working_dir)
    seconds = time.perf_counter() - t0
    _, ended = _events(output)
    skipped = _skip_reasons(output)
    results, alone, rest = {}, [], []
    names = list(dict.fromkeys(batch.names))
    for name in names:
        ev = ended.get(name)
        if ev is None and killed_for is not _OUTSIDE_A_TEST and name == killed_for:
            results[name] = TestResult(
                name,
                ERROR,
                detail=f"still running after {batch.timeout_for(name):.0f}s; the process was killed\n{_human(output)}".strip(),
            )
        elif ev is None:
            (rest if killed_for is not None else alone).append(name)
        elif ev.get("status") == "error" and len(names) > 1:
            alone.append(name)
        else:
            results[name] = _classify(name, ev, _human(output) if ev.get("status") != "ok" else "", skipped.get(name))

    unexplained = None
    if killed_for is _OUTSIDE_A_TEST:
        unexplained = f"the process hung while no test was running (limit {batch.timeout_s:.0f}s) and was killed"
    elif (
        killed_for is None
        and returncode != 0
        and all(n in ended for n in names)  # a missing end event is a crash, and has its own path below
        and not any(ev.get("status") == "error" for ev in ended.values())
    ):
        unexplained = f"the binary ended with exit code {returncode} although no test reported a failure"
    if unexplained:
        for name in names:
            if name not in results or results[name].outcome == PASSED:
                results[name] = TestResult(name, ERROR, detail=f"{unexplained}\n{_human(output)}".strip())
        return results

    if len(names) == 1 and alone:
        name = alone.pop()
        results[name] = TestResult(
            name, ERROR, detail=f"the binary ended without an end event for this test\n{_human(output)}".strip()
        )
    if rest and len(rest) < len(names):
        results.update(run_batch(binary, _sub(batch, "rest", rest), working_dir))
    for name in alone:
        again = run_batch(binary, _sub(batch, "alone-" + re.sub(r"[^A-Za-z0-9._-]+", "_", name), [name]), working_dir)[
            name
        ]
        if name in ended and again.outcome == PASSED:  # it failed in the batch and passes alone
            again = TestResult(
                name,
                FAILED,
                detail="failed when run with the other tests of its batch, and passed when run alone: "
                f"one of them leaves state that breaks it. The batch output is in {os.path.join(batch.dir, 'output.txt')}",
            )
        results[name] = again
    for name in names:  # a name the binary never mentioned at all
        results.setdefault(
            name, TestResult(name, ERROR, detail=f"the binary reported nothing for this test\n{_human(output)}".strip())
        )
    if len(names) == 1:
        results[names[0]].seconds = round(seconds, 2)
    return results


def list_binary_tests(binary, working_dir, scratch):
    """The names the binary knows, as paths relative to `working_dir`. An extension build lists an
    extension's tests by relative and by absolute path; both forms fold into one."""
    os.makedirs(scratch, exist_ok=True)
    # The pattern "*" also lists hidden tests (.test_slow, .test_coverage); a bare -l leaves them out.
    p = subprocess.run(
        [binary, "-l", "*", "--temp-dir-root", scratch], cwd=working_dir, capture_output=True, text=True, timeout=120
    )
    if "\t" not in p.stdout:
        # The exit code of -l is the test count, so it says nothing; no listing at all says the binary
        # did not start (for example Catch refuses a directory name that gives a tag it reserves).
        raise UsageError(f"`{os.path.basename(binary)} -l` listed nothing:\n{(p.stderr or p.stdout).strip()[:600]}")
    names = set()
    root = os.path.realpath(working_dir)
    for line in p.stdout.splitlines():
        name = line.split("\t")[0].strip()
        if not name.endswith((".test", ".test_slow", ".test_coverage")):
            continue
        real = os.path.realpath(os.path.join(working_dir, name))
        if real.startswith(root + os.sep):
            names.add(os.path.relpath(real, root))
    return names


def check_against_binary(scanned_paths, binary, working_dir, scratch, scan_root, excluded=()):
    """Both sides in one scope: every `.test` the scan found under `scan_root` against every test the
    binary knows under it. A file on disk that the binary does not know would never run; a test the
    binary knows that is not on disk is a stale build."""
    root_rel = os.path.relpath(scan_root, working_dir)
    inside = lambda n: root_rel in (".", "") or n == root_rel or n.startswith(root_rel.rstrip("/") + "/")  # noqa: E731
    left_out = {os.path.normpath(p) for p in excluded}
    known = {n for n in list_binary_tests(binary, working_dir, scratch) if inside(n) and n not in left_out}
    scanned = {os.path.normpath(p) for p in scanned_paths if inside(os.path.normpath(p))}
    problems = []
    if only_disk := sorted(scanned - known):
        problems.append(
            f"{len(only_disk)} test file(s) on disk that the binary does not know (rebuild?): "
            + ", ".join(only_disk[:5])
        )
    if only_binary := sorted(known - scanned):
        problems.append(
            f"{len(only_binary)} test(s) the binary knows that are not on disk (stale build?): "
            + ", ".join(only_binary[:5])
        )
    return problems
