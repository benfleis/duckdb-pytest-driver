"""The `run` runner: DuckDB's own parallel wrapper, `build/<build>/test/run`, as ducktest's runner.

`--runner run` hands every `.test` batch to the wrapper instead of to ducktest's own pool of
`unittest` processes, so that a run gets what the wrapper has for CI: its batching, its worker
count, its retry and its batch timeout. Its own options are passed through as they are, with
`--run-args '...'`, and are not re-invented here.

ducktest gives it one call for each set of batches that share a test environment, because a call
takes one `--test-config`:

    run/g001/tests.txt     every test of those batches           (--test-list)
    run/g001/config.json   the environment they share            (inside --test-flags)
    run/g001/output.txt    what the wrapper printed
    run/g001/processes/    one file for each unittest process the wrapper started

The last of these is how a test gets a real outcome. The wrapper forwards the output of a failing
batch only, so a test that passed leaves nothing in what it prints; but it takes `--test-command`,
the template it runs a batch with, and the test list in that template is a new file for every batch.
So ducktest puts a two-line shim in front of the binary that writes the process's own output to
`processes/` and passes it through. The binary's `--emit-test-events` lines are then there for every
test, passed or failed, and an outcome is as strong as under the `unittest` runner (`evidence:
event`).

A test the shim's files say nothing about -- a process the wrapper killed at its batch timeout, a
crash before the first event -- falls back to what the wrapper's own text and exit code say: passed
with no seconds when the wrapper says every test passed, an error with the wrapper's text when it
does not (`evidence: run-summary`). A report says so in one line when any test was decided that way.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import sys

from . import sqllogic
from .binary import UsageError
from .outcomes import ERROR, PASSED

_log = logging.getLogger(__name__)

EVENT, SUMMARY = "event", "run-summary"

# "[3/10] (30%): test/sql/x.test took 0.44s": the binary's own progress line, and the only place it
# says how long one test took (an end event has no duration yet: request R1). It is not matched at
# the start of a line: the line has no newline of its own, so what stderr writes lands in the middle.
_TOOK = re.compile(r"\[\d+/\d+\] \(\d+%\): (\S+) took ([0-9]+(?:\.[0-9]+)?)s")

# Runs one batch of the wrapper and keeps the process's own output. `{test_list}` is a new file for
# each batch, so no two processes write the same name.
_SHIM = '''\
"""Runs one unittest process for DuckDB's `run` wrapper and keeps its output.

argv: OUT_DIR NAME_FROM THE_COMMAND ...; the output goes to OUT_DIR/<name>.txt and on to stdout, and
the exit code is the binary's own, so the wrapper sees the batch exactly as it would without this.
"""

import os
import subprocess
import sys

out_dir, name_from, command = sys.argv[1], sys.argv[2], sys.argv[3:]
os.makedirs(out_dir, exist_ok=True)
path = os.path.join(out_dir, os.path.basename(name_from) + ".txt")
with open(path, "wb") as kept:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for chunk in iter(lambda: process.stdout.read(4096), b""):
        kept.write(chunk)
        sys.stdout.buffer.write(chunk)
    code = process.wait()
sys.stdout.flush()
raise SystemExit(code)
'''


def find_wrapper(unittest_bin, *, explicit=None):
    """`run` beside the unittest binary, which is where a DuckDB build puts it."""
    path = os.path.abspath(explicit or os.path.join(os.path.dirname(unittest_bin), "run"))
    if not os.path.isfile(path):
        raise UsageError(
            f"DuckDB's run wrapper was not found at {path}. It is built beside test/unittest; pass --run-bin PATH, "
            "or use --runner unittest."
        )
    return path


def _groups(units):
    """The batches, gathered by the test environment they share: one wrapper call takes one
    `--test-config`. A file is in a call once, as in a batch, so that the binary's events can be
    told apart; a second test of one file goes into another call."""
    groups, keys = [], {}
    for tests, batch in units:
        key = batch.test_env.key()
        for n in keys.get(key, []):
            if not {c.definition.body_file for c in tests} & {c.definition.body_file for c in groups[n][0]}:
                groups[n][0].extend(tests)
                break
        else:
            keys.setdefault(key, []).append(len(groups))
            groups.append(([*tests], batch.test_env))
    return groups


def _shim_command(group_dir):
    """What the wrapper starts for one batch: the shim, then the command it would have started."""
    shim = os.path.join(group_dir, "shim.py")
    with open(shim, "w") as f:
        f.write(_SHIM)
    parts = [sys.executable, shim, os.path.join(group_dir, "processes"), "{test_list}"]
    return " ".join(shlex.quote(p) for p in parts) + " {binary} {flags} -f {test_list}"


def _kept_output(group_dir):
    """What each unittest process printed, oldest first: a later run of one test is a later file."""
    processes = os.path.join(group_dir, "processes")
    if not os.path.isdir(processes):
        return []
    paths = sorted((os.path.join(processes, n) for n in os.listdir(processes)), key=lambda p: (os.path.getmtime(p), p))
    return [open(p, errors="replace").read() for p in paths]


def _results(group, texts, wrapper_text, wrapper_code):
    """{test name: TestResult} for every test of the group. What an event says wins; what no event
    mentions is decided by the wrapper's summary, which knows of no single test."""
    results, attempts = {}, {}
    for text in texts:
        _, ended = sqllogic.test_events(text)
        skipped = sqllogic.skip_reasons(text)
        took = {name: float(seconds) for name, seconds in _TOOK.findall(text)}
        for name, ev in ended.items():
            if name not in group.names:
                continue
            detail = sqllogic.human(text) if ev.get("status") != "ok" else ""
            result = sqllogic.classify(group, name, ev, detail, skipped.get(name))
            result.evidence = EVENT
            result.seconds = took.get(name)
            attempts[name] = attempts.get(name, 0) + 1
            result.attempts = attempts[name]
            if attempts[name] > 1 and result.outcome == PASSED:
                result.note = f"passed on attempt {attempts[name]}, which the run wrapper asked for"
            results[name] = result
    for name in group.names:
        if name in results:
            continue
        if wrapper_code == 0:
            # Every test of the call passed, says the wrapper; it says nothing about one test.
            results[name] = sqllogic.TestResult(name, PASSED, evidence=SUMMARY)
        else:
            results[name] = sqllogic.TestResult(
                name,
                ERROR,
                detail="the run wrapper reported no event for this test and did not end green:\n" + wrapper_text,
                evidence=SUMMARY,
            )
    return results


def _call(args, working_dir, env):
    """The wrapper as a child of this process, killed with it when the run is stopped."""
    _log.debug("run: %s", shlex.join(args))
    proc = subprocess.Popen(
        args, cwd=working_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    with sqllogic.watched(proc):
        return proc.communicate()[0] or "", proc.wait()


def run_wrapper(unittest_bin, units, working_dir, *, run_dir, workers=4, run_args="", run_bin=None):
    """Run every batch through the wrapper. `units` is [(tests, batch)] as the exec phase made them;
    returns [(tests, the batch of the wrapper call, {test id: TestResult})], one for each call."""
    wrapper = find_wrapper(unittest_bin, explicit=run_bin)
    out = []
    for n, (tests, test_env) in enumerate(_groups(units), 1):
        # Absolute: the wrapper runs from the DuckDB source root, not from where ducktest was started.
        group_dir = os.path.abspath(os.path.join(run_dir, "run", f"g{n:03d}"))
        group = sqllogic.Batch(group_dir, [(c.id, c.definition.body_file) for c in tests], test_env)
        group.write()
        flags = [
            "--test-dir",
            os.path.abspath(working_dir),
            "--test-config",
            os.path.join(group_dir, "config.json"),
            "--emit-test-events",
            "--emit-on-skip",
        ]
        args = [
            wrapper,
            "--test-list",
            os.path.join(group_dir, "tests.txt"),
            "--workers",
            str(max(1, workers)),
            "--test-flags",
            " ".join(shlex.quote(f) for f in flags),
            "--test-command",
            _shim_command(group_dir),
            *shlex.split(run_args),
        ]
        _log.info("batch %s: starting, %d test(s)", os.path.basename(group_dir), len(tests))
        done = _call(args, working_dir, group.test_env.environ())
        text, code = done
        _log.info("batch %s: finished, %d test(s)", os.path.basename(group_dir), len(tests))
        with open(os.path.join(group_dir, "output.txt"), "w") as f:
            f.write(text)
        by_name = _results(group, _kept_output(group_dir), text, code)
        out.append((tests, group, {c.id: by_name[c.definition.body_file] for c in tests}))
    return out
