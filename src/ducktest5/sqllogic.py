"""The `unittest` runner: `.test` bodies, in batches, through DuckDB's own binary.

A batch is two files in its own directory, and that is the whole contract with the binary:

    tests.txt     one test path per line (the binary's exact `-f` mode: only these are registered)
    config.json   the `--test-config` file: `{NAME}` for the body, the roots, the init file

Everything in them comes from the test environment the batch was made with, and every test in a
batch has the same one. The binary applies the config before it runs anything, so an extension is
not there yet; the init file is where a setting and a secret type of an extension can be reached,
after a `require` for it.

Every test must end with an event from the binary that says how it ended. A test with no such event
did not run as far as this runner knows, and is never counted as passed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .binary import find_built
from .outcomes import ERROR, FAILED, PASSED, SKIPPED
from .resources.dialect import render
from .testenv import TestEnv

_log = logging.getLogger(__name__)

_EVENT = re.compile(r"\[TEST_EVENT\] (\{.*\})")
_SKIP = re.compile(r"\[SKIP_TEST\] (.*?) :: (.*)")
_PROGRESS = re.compile(r"^\[\d+/\d+\] \(\d+%\): .*$", re.M)
_LOCATION = re.compile(r"([^\s():]+):(\d+)")  # "file.test:12", as the binary names where a failure is
DEFAULT_TIMEOUT_S = 600  # for one test, .test or Python, unless `timeout=` in conf.py says otherwise
_OUTSIDE_A_TEST = object()  # the process hung while no test was running: at startup, between tests, at exit
INIT_FILE = "init.sqllogic"

# The binaries this process started and has not reaped. A run that is stopped from outside kills
# them, so that nothing of it is left running when it is gone, and starts no more.
_running = set()
_stopped = threading.Event()


def kill_running():
    _stopped.set()
    for proc in list(_running):
        try:
            proc.kill()
        except Exception:
            pass


def start_running():
    """A new run in this process may start binaries again."""
    _stopped.clear()


def stopped():
    return _stopped.is_set()


@contextlib.contextmanager
def watched(proc):
    """A binary of this run, for as long as it runs."""
    _running.add(proc)
    try:
        yield proc
    finally:
        _running.discard(proc)


def find_unittest(*, explicit=None, build="debug", places=(".",)):
    return find_built(
        "test/unittest", "unittest binary", flag="--unittest-bin", explicit=explicit, build=build, places=places
    )


@dataclass
class TestResult:
    name: str
    outcome: str
    seconds: float | None = None
    detail: str = ""
    kept: str | None = None
    note: str = ""
    attempts: int = 1


def init_file_text(test_env):
    """The init file, and {line of a statement header: what asked for that statement}.

    The requires come first, as `require`, which is the binary's own way to load an extension: a
    `SET` or a secret type of one fails before that. Then the scoped secrets, then the statements,
    one block each, so a failure can be traced back to the one thing that asked for it."""
    lines, sources = [], {}
    extensions = test_env.extensions + [e.value for e in test_env.secret_requires]
    for line in render("unittest", "requires", extensions).sql:
        lines += [line, ""]
    for entry in [*test_env.secret_sql, *test_env.init_sql]:
        lines.append("statement ok")
        sources[len(lines)] = entry.source
        lines += [line for line in entry.value.splitlines() if line.strip()] + [""]
    while lines and lines[-1] == "":
        lines.pop()
    return ("\n".join(lines) + "\n" if lines else ""), sources


@dataclass
class Batch:
    dir: str
    # (key, test name) pairs. The binary knows a test by its name; the caller knows it by the key, and
    # gets its result, and gives its timeout and retries, by the key. A bare name is its own key.
    tests: list
    test_env: object = None  # the TestEnv every test in this batch is given
    timeout_s: float = DEFAULT_TIMEOUT_S  # for one test, counted from the binary's begin event for it
    timeouts: dict = field(default_factory=dict)  # key -> seconds, where a test has its own
    retries: dict = field(default_factory=dict)  # key -> how often a failure may run again
    # The binary's RUN_ID, so its TEMP_DIR is `temp_dir_root/<batch_token>/<test id>`; the dir name by default.
    batch_token: str | None = None

    def __post_init__(self):
        self.tests = [(t, t) if isinstance(t, str) else tuple(t) for t in self.tests]
        self._key = {name: key for key, name in self.tests}
        self.test_env = self.test_env if self.test_env is not None else TestEnv()
        self.init_sources = {}  # line of the init file -> source; filled by write()
        self.batch_token = self.batch_token or os.path.basename(self.dir.rstrip("/"))
        if len(self._key) != len(self.tests):
            # The binary reports by name, so two tests of one file could not be told apart.
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
            "run_id": self.batch_token,
            "test_env": [{"env_name": k, "env_value": str(v)} for k, v in sorted(self.test_env.names.items())],
            # By default the binary ends a file early, as not failed, when an error mentions 'HTTP' or
            # 'Unable to connect'. Here an error is a failure; a test that may fail for reasons that are
            # not its own asks for `retries`.
            "skip_error_messages": [],
        }
        config.update(self.test_env.root_paths)
        if self.test_env.passthrough:
            config["env_passthrough"] = sorted(self.test_env.passthrough)  # names only: values stay in the process env
        text, self.init_sources = init_file_text(self.test_env)
        if text:
            init = os.path.join(self.dir, INIT_FILE)
            # For the owner only: the init SQL of a MATRIX value may hold the secret of a service.
            with open(os.open(init, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(text)
            config["init_sqllogic"] = init
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump(config, f, indent=1)


def remove_init_sql(root):
    """Take the init SQL out of every batch under `root`, for a directory that stays after the run:
    it may hold a secret. The file stays, and says how to get its content again."""
    for dirpath, _, filenames in os.walk(root):
        if INIT_FILE in filenames:
            with open(os.path.join(dirpath, INIT_FILE), "w") as f:
                f.write(
                    "# The init SQL of this batch was removed after the run: it may hold a secret.\n"
                    "# Run the test again with --keep to get it.\n"
                )


def test_events(output):
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


def skip_reasons(output):
    """{test name: reason} from the binary's skip lines. The end event says `ok` for a file that the
    binary gave up on (an error that matches its skip list, a `mode skip`), so the reason is here only."""
    return {name.strip(): reason.strip() for name, reason in _SKIP.findall(output)}


def human(output):
    """The binary's own text, without the event lines, the skip lines and the progress lines."""
    text = _EVENT.sub("", output)
    text = _SKIP.sub("", text)
    text = _PROGRESS.sub("", text)
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip()).strip()


def batch_args(binary, batch, working_dir="."):
    """How the binary is started for a batch; a debugger is given the same."""
    return [
        binary,
        # else the binary resolves every test path against the source root it was built in
        "--test-dir",
        os.path.abspath(working_dir),
        "-f",
        os.path.join(batch.dir, "tests.txt"),
        "--test-config",
        os.path.join(batch.dir, "config.json"),
        "--emit-test-events",
        "--emit-on-skip",
    ]


def _invoke(binary, batch, working_dir):
    """Run the batch and watch it: a test that is still running after its timeout gets the process
    killed. Returns (output, what was running when it was killed or None, the exit code, and
    {test name: seconds between its begin and end events})."""
    if _stopped.is_set():
        # The run was stopped: the tests of this batch end with no event, which is what a resume
        # takes up again.
        raise RuntimeError("the run was stopped")
    batch.write()
    args = batch_args(binary, batch, working_dir)
    _log.debug("unittest: %s", shlex.join(args))
    proc = subprocess.Popen(
        args,
        cwd=working_dir,
        env=batch.test_env.environ(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    lines = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(line) for line in proc.stdout] and None or lines.put(None), daemon=True
    ).start()
    output, running, since, killed_for, took = [], None, time.perf_counter(), None, {}
    _running.add(proc)
    if _stopped.is_set():  # the run was stopped between the check above and the start
        proc.kill()
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
    _running.discard(proc)
    text = "".join(output)
    with open(os.path.join(batch.dir, "output.txt"), "w") as f:
        f.write(text)
    return text, killed_for, returncode, took


def _init_statements(path):
    """{line of a statement header, 1-based: the SQL under it} for a sqllogic file."""
    blocks, header = {}, None
    for n, line in enumerate((x.rstrip() for x in open(path).read().splitlines()), 1):
        if line.split(" ")[0] in ("statement", "query"):
            header, blocks[n] = n, []
        elif not line.strip() or line.startswith("----"):
            header = None
        elif header:
            blocks[header].append(line)
    return {n: "\n".join(sql) for n, sql in blocks.items() if sql}


def _in_the_init_sql(batch, detail):
    """The init file's line the failure is on, or None.

    The binary runs the init file under the body file's name (`command->file_name = script`), so a
    statement of the init file that fails is reported as `<body>.test:<line of the init file>` and
    sends the reader to the wrong file. It is ours when the reported line holds a statement of the
    init file and that statement's text is in what the binary printed."""
    if not batch.init_sources:
        return None
    try:
        blocks = _init_statements(os.path.join(batch.dir, INIT_FILE))
    except OSError:
        return None
    reported = {int(line) for _, line in _LOCATION.findall(detail)}
    return next((n for n in sorted(reported & set(blocks)) if blocks[n] in detail), None)


def classify(batch, name, ev, detail, skip_reason=None):
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
    detail = (ev.get("data") or "") + ("\n" + detail if detail else "")
    if line := _in_the_init_sql(batch, detail):
        where = batch.init_sources.get(line)
        # Not FAILED: the body never ran, so it proved nothing.
        return TestResult(
            name,
            ERROR,
            detail=f"in the init SQL of {where} ({os.path.join(batch.dir, INIT_FILE)}:{line})\n{detail}",
            kept=kept,
        )
    return TestResult(name, FAILED, detail=detail, kept=kept)


def _sub(batch, tag, names):
    return Batch(
        os.path.join(batch.dir, tag),
        [(batch._key[name], name) for name in names],
        batch.test_env,
        batch.timeout_s,
        batch.timeouts,
        batch.retries,
        f"{batch.batch_token}-{tag}",
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
        while result.outcome in (FAILED, ERROR) and attempt < allowed and not _stopped.is_set():
            attempt += 1
            result = _run_once(binary, _sub(batch, f"attempt{attempt}-{_safe(name)}", [name]), working_dir)[name]
        if result.outcome == PASSED and attempt > 1:
            result.note = "; ".join(x for x in (result.note, f"passed on attempt {attempt} of {allowed}") if x)
            results[name] = result
        else:
            result = first
        if attempt > 1:
            _log.warning("%s: retried (%d of %d attempts), ended %s", name, attempt, allowed, result.outcome)
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
    _, ended = test_events(output)
    skipped = skip_reasons(output)
    results, alone, rest = {}, [], []
    names = batch.names
    for name in names:
        ev = ended.get(name)
        if ev is None and killed_for is not _OUTSIDE_A_TEST and name == killed_for:
            results[name] = TestResult(
                name,
                ERROR,
                detail=f"still running after {batch.timeout_for(name):.0f}s; the process was killed\n{human(output)}".strip(),
            )
        elif ev is None:
            (rest if killed_for is not None else alone).append(name)
        elif ev.get("status") == "error" and len(names) > 1:
            alone.append(name)
        else:
            results[name] = classify(
                batch, name, ev, human(output) if ev.get("status") != "ok" else "", skipped.get(name)
            )
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
                results[name] = TestResult(name, ERROR, detail=f"{unexplained}\n{human(output)}".strip())
        return results

    if len(names) == 1 and alone:
        name = alone.pop()
        results[name] = TestResult(
            name, ERROR, detail=f"the binary ended without an end event for this test\n{human(output)}".strip()
        )
    if _stopped.is_set():
        return results
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
            name, TestResult(name, ERROR, detail=f"the binary reported nothing for this test\n{human(output)}".strip())
        )
    if len(names) == 1 and results[names[0]].seconds is None:  # no end event: what the process took
        results[names[0]].seconds = round(seconds, 2)
    return results
