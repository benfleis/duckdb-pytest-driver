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
_INIT_FILE = "init.sqllogic"


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
    attempts: int = 1


@dataclass
class Batch:
    dir: str
    # (key, test name) pairs. The binary knows a test by its name; the caller knows it by the key, and
    # gets its result, and gives its timeout and retries, by the key. A bare name is its own key.
    tests: list
    env: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    init_sql: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S  # for one test, counted from the binary's begin event for it
    timeouts: dict = field(default_factory=dict)  # key -> seconds, where a test has its own
    retries: dict = field(default_factory=dict)  # key -> how often a failure may run again

    def __post_init__(self):
        self.tests = [(t, t) if isinstance(t, str) else tuple(t) for t in self.tests]
        self._key = {name: key for key, name in self.tests}
        if len(self._key) != len(self.tests):
            # The binary reports by name, so two cases of one file could not be told apart.
            raise ValueError(f"a test can be in a batch once: {self.names}")

    @property
    def names(self):
        return [name for _, name in self.tests]

    def timeout_for(self, name):
        return self.timeouts.get(self._key[name]) or self.timeout_s

    def retries_for(self, name):
        return self.retries.get(self._key[name], 0)

    def write(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "tests.txt"), "w") as f:
            f.write("".join(n + "\n" for n in self.names))
        config = {
            "description": "a ducktest batch",
            "temp_dir_root": os.path.join(self.dir, "tmp"),
            "test_env": [{"env_name": k, "env_value": str(v)} for k, v in sorted(self.env.items())],
            "settings": [{"name": k, "value": str(v)} for k, v in sorted(self.settings.items())],
            # By default the binary ends a file early, as not failed, when an error mentions 'HTTP' or
            # 'Unable to connect'. Here an error is a failure; a test that may fail for reasons that are
            # not its own asks for `retries`.
            "skip_error_messages": [],
        }
        if self.init_sql:
            init = os.path.join(self.dir, _INIT_FILE)
            # For the owner only: the init SQL of a variant may hold the secret of a service.
            with open(os.open(init, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write("statement ok\n" + self.init_sql.strip().rstrip(";") + ";\n")
            config["init_sqllogic"] = init
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump(config, f, indent=1)


def remove_init_sql(root):
    """Take the init SQL out of every batch under `root`, for a directory that stays after the run:
    it may hold a secret. The file stays, and says how to get its content again."""
    for dirpath, _, filenames in os.walk(root):
        if _INIT_FILE in filenames:
            with open(os.path.join(dirpath, _INIT_FILE), "w") as f:
                f.write(
                    "# The init SQL of this batch was removed after the run: it may hold a secret.\n"
                    "# Run the case again with --keep to get it.\n"
                )


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
    killed. Returns (output, what was running when it was killed or None, the exit code, and
    {test name: seconds between its begin and end events})."""
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
    output, running, since, killed_for, took = [], None, time.perf_counter(), None, {}
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
                    if running == ev.get("name"):
                        took[running] = round(time.perf_counter() - since, 2)
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
    return text, killed_for, returncode, took


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
        [(batch._key[name], name) for name in names],
        batch.env,
        batch.settings,
        batch.init_sql,
        batch.timeout_s,
        batch.timeouts,
        batch.retries,
    )


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def run_batch(binary, batch, working_dir):
    """Run one batch; returns {key: TestResult}, one for every test. A test with `retries` that ended
    FAILED or ERROR runs again alone, up to that many times. The first attempt that passes makes it
    PASSED, with the attempt in its note and in `attempts`; when none passes, the first result that is
    the test's own stays. For such a test the run alone after a failure in a batch is attempt 2."""
    results = _run_once(binary, batch, working_dir)
    for name, first in list(results.items()):
        allowed = 1 + batch.retries_for(name)
        result, attempt = first, first.attempts
        while result.outcome in (FAILED, ERROR) and attempt < allowed:
            attempt += 1
            result = _run_once(binary, _sub(batch, f"attempt{attempt}-{_safe(name)}", [name]), working_dir)[name]
        if result.outcome == PASSED and attempt > 1:
            result.note = "; ".join(x for x in (result.note, f"passed on attempt {attempt} of {allowed}") if x)
            results[name] = result
        else:
            result = first
        result.attempts = attempt
    return {batch._key[name]: result for name, result in results.items()}


def _run_once(binary, batch, working_dir):
    """One batch, with no retries; returns {name: TestResult}, one for every name.

    - An end event is the only thing that gives a test an outcome here. A test that was still running
      at its timeout is an ERROR. A test with no end event runs again: the ones after a killed test as
      a new batch, the others alone; with no end event alone, it is an ERROR.
    - A test that failed inside a batch of several runs again alone for its text only. Its outcome
      stays FAILED also when it passes alone: a failure that needs the other files of the batch is
      state that leaks between tests, which is a failure. Only a test with `retries` may pass there.
    - When the process hung while no test was running, or ended with an exit code that the end events
      do not explain, every test of the batch that would count as passed is an ERROR instead.
    """
    t0 = time.perf_counter()
    output, killed_for, returncode, took = _invoke(binary, batch, working_dir)
    seconds = time.perf_counter() - t0
    _, ended = _events(output)
    skipped = _skip_reasons(output)
    results, alone, rest = {}, [], []
    names = batch.names
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
            results[name].seconds = took.get(name)

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
        results.update(_run_once(binary, _sub(batch, "rest", rest), working_dir))
    for name in alone:
        again = _run_once(binary, _sub(batch, "alone-" + _safe(name), [name]), working_dir)[name]
        if batch.retries_for(name):
            again.attempts = 2
        elif name in ended and again.outcome == PASSED:  # it failed in the batch and passes alone
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
    if len(names) == 1 and results[names[0]].seconds is None:  # no end event: what the process took
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
