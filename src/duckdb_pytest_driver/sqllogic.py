"""The SQLLogic `.test` lane: collect & run `.test` files through the unittest binary.

One `.test` file -> one SqlLogicItem; the item invokes the Catch2 unittest binary with the
test path as a name filter, batched via `--batch-size` (batch assignment lives in
driver.plugin's pytest_collection_modifyitems). Pass/skip/fail is parsed from the binary's
name-tagged `[TEST_EVENT]` stream (--emit-test-events), so outcomes are attributable per test
even inside a batch. Binary resolution, `run_paired` (driving a same-stem body), and all hooks
live in driver.plugin.
"""

import json
import os
import shlex
import subprocess
import tempfile
import threading
import warnings

import pytest


# ---------------------------------------------------------------------------
# Per-process batch cache (coherent within a worker; xdist_group keeps a
# batch on one worker, so this is always correct).
# ---------------------------------------------------------------------------

_batch_cache: dict = {}  # batch_id → {test_name: result_dict}
_batch_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class SqlLogicFile(pytest.File):
    """One .test file → one SqlLogicItem."""

    @classmethod
    def from_parent(cls, parent, *, binary, working_dir, **kwargs):
        obj = super().from_parent(parent, **kwargs)
        obj._binary = binary
        obj._working_dir = working_dir
        return obj

    def collect(self):
        from .plugin import _run_dir  # lazy: plugin imports this lane (avoid import cycle)
        test_name = os.path.relpath(str(self.path), self._working_dir)
        yield SqlLogicItem.from_parent(
            self,
            name=self.path.stem,
            test_name=test_name,
            binary=self._binary,
            working_dir=self._working_dir,
            temp_dir_base=_run_dir(self.config),
        )


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class SqlLogicItem(pytest.Item):
    @classmethod
    def from_parent(cls, parent, *, test_name, binary, working_dir, temp_dir_base=None, **kwargs):
        obj = super().from_parent(parent, **kwargs)
        obj._test_name = test_name
        obj._binary = binary
        obj._working_dir = working_dir
        obj._temp_dir_base = temp_dir_base
        obj._batch_id = None
        obj._batch_test_names = None
        return obj

    def runtest(self):
        if self._batch_id is not None:
            self._run_batched()
        else:
            self._run_single()

    # -- single-test path (batch_size == 1) ----------------------------------

    def _run_single(self):
        result = _invoke(
            self._binary,
            [self._test_name],
            self._working_dir,
            self._temp_dir_base,
            extra_args=resolve_unittest_args(self.config),
        )
        _raise_for_result(_parse_result(result))

    # -- batch path (batch_size > 1) -----------------------------------------

    def _run_batched(self):
        with _batch_cache_lock:
            if self._batch_id not in _batch_cache:
                _batch_cache[self._batch_id] = _execute_batch(
                    self._batch_test_names,
                    self._binary,
                    self._working_dir,
                    self._temp_dir_base,
                    extra_args=resolve_unittest_args(self.config),
                )
        r = _batch_cache[self._batch_id].get(self._test_name, {"status": "internal_error"})
        _raise_for_result(r)

    # -- pytest protocol -------------------------------------------------

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, SqlLogicFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, None, self._test_name


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


def _execute_batch(
    test_names: list, binary: str, working_dir: str, temp_dir_base: str = None, extra_args: list = None
) -> dict:
    """Run a batch.  On failure, re-run individually for per-test attribution."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        tmpfile = f.name
        for name in test_names:
            f.write(name + "\n")
    try:
        result = _invoke(
            binary,
            [
                "-f",
                tmpfile,
                "--start-offset",
                "0",
                "--end-offset",
                str(len(test_names)),
            ],
            working_dir,
            temp_dir_base,
            extra_args=extra_args,
        )
    finally:
        os.unlink(tmpfile)

    # Per-test outcome is attributable from the name-tagged [TEST_EVENT] stream, so a clean batch
    # needs no re-run. A test the events mark failed/incomplete is re-run alone to get a clean,
    # per-test diff (the batch's combined output isn't sliced per test).
    events = _scan_test_events(result["stdout"] + result["stderr"])
    statuses = {}
    for name in test_names:
        st = _classify(events, name, result)
        if st["status"] in ("fail", "internal_error"):
            st = _invoke_single(name, binary, working_dir, temp_dir_base, extra_args=extra_args)
        statuses[name] = st
    return statuses


def _invoke_single(
    test_name: str, binary: str, working_dir: str, temp_dir_base: str = None, extra_args: list = None
) -> dict:
    result = _invoke(binary, [test_name], working_dir, temp_dir_base, extra_args=extra_args)
    return _parse_result(result)


# ---------------------------------------------------------------------------
# Low-level subprocess helpers
# ---------------------------------------------------------------------------


def resolve_unittest_args(config) -> list:
    """Flatten the repeatable --unittest-args option into binary argv tokens.

    Each value is shlex-split, so --unittest-args='--test-config x.json' contributes two
    tokens. Read lazily (at run time, not collection) so a consuming repo's conftest can set
    the default in pytest_configure regardless of hook order. Both lanes (collector items and
    run_paired) route the result through _invoke.
    """
    tokens = []
    for value in config.getoption("--unittest-args", default=[]) or []:
        tokens.extend(shlex.split(value))
    return tokens


def _invoke(
    binary: str, args: list, working_dir: str, temp_dir_base: str = None, env: dict = None, extra_args: list = None
) -> dict:
    # Opt into the binary's per-test event stream on every invocation (single + batch). Requires the
    # C++ --emit-test-events flag to be built into the binary, else Catch2 errors on the unknown arg.
    # extra_args (the --unittest-args passthrough) follow, before the test name / -f selectors.
    args = ["--emit-test-events", *(extra_args or []), *args]
    if temp_dir_base:
        # Caller-owned per-run base (BASE/<run-id>); pytest owns its lifecycle. RUN_ID is passed
        # explicitly (== the run-id in the base) so the binary's RUN_ID env matches pytest's, but
        # it's NOT added as a path level (--temp-dir-run-id off — the base already carries it). The
        # binary places a per-test subdir and never destroys it; pytest removes the run dir at
        # sessionfinish.
        args = [
            "--temp-dir-base",
            str(temp_dir_base),
            "--run-id",
            os.path.basename(str(temp_dir_base)),
            "--temp-dir-run-id",
            "off",
            "--temp-dir-destroy",
            "never",
            *args,
        ]
    # env (e.g. provisioned UC_TEST_CATALOG/SCHEMA) is merged over the ambient env so
    # the body's ${...} substitution resolves to the provisioned values; None inherits.
    run_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(
            [binary] + args,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
        )
    except FileNotFoundError:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"unittest binary not found: {binary}\n" "Build the extension first (e.g. make debug).",
        }
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="replace"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
    }


# The unittest binary emits a per-test JSON event stream on stderr (gated by --emit-test-events,
# passed by _invoke). Each line is `[TEST_EVENT] <json-object>` — a flare prefix + one JSON object,
# so it survives interleaved stderr and is attributable per test even inside a batch:
#   [TEST_EVENT] {"event":"begin","name":<test>}
#   [TEST_EVENT] {"event":"end","name":<test>,"status":<ok|error|skip-requirement>,
#                 "passes":N,"fails":N,"skip-mode":N[,"data":<reason|error text>]}
# Only `end` carries the outcome; statement pass/fail/skip are counted into the tally, not emitted.
_FLARE = "[TEST_EVENT] "


def _scan_test_events(output: str) -> dict:
    """Map test_name -> its `end` event object for every [TEST_EVENT] line in the output.

    Robust to prefixes / interleaved stderr: find the flare anywhere on the line and json.loads the
    object after it (once per test, not per statement — cheap). `begin` events are ignored (they
    only mark that a test started, for crash detection).
    """
    tests: dict = {}
    for line in output.splitlines():
        i = line.find(_FLARE)
        if i < 0:
            continue
        try:
            ev = json.loads(line[i + len(_FLARE):])
        except ValueError:
            continue
        if ev.get("event") == "end":
            tests[ev.get("name")] = ev
    return tests


def _classify(events: dict, name, result: dict) -> dict:
    """Resolve one test's pass/skip/fail from its `end` event, falling back to the return code."""
    combined = result["stdout"] + result["stderr"]
    ev = events.get(name)
    if ev is None:
        # No terminal event for this test (e.g. a crash before `end`, or an old binary): trust rc.
        if result.get("returncode", 1) != 0:
            return {"status": "fail", "output": combined}
        return {"status": "pass", "stats": {}}
    stats = {"passes": ev.get("passes", 0), "fails": ev.get("fails", 0), "skip-mode": ev.get("skip-mode", 0)}
    status = ev.get("status")
    if status == "skip-requirement":
        return {"status": "skip", "reason": ev.get("data") or "skipped", "stats": stats}
    if status == "error":
        # `data` carries the message for infra errors; a statement assertion fail has none, so fall
        # back to the captured output (the full diff lives there).
        return {"status": "fail", "output": ev.get("data") or combined, "stats": stats}
    return {"status": "pass", "stats": stats}


def _parse_result(result: dict) -> dict:
    """Classify a single-test invocation from its [TEST_EVENT] stream."""
    events = _scan_test_events(result["stdout"] + result["stderr"])
    name = next(iter(events), None)
    return _classify(events, name, result)


def _raise_for_result(result: dict) -> None:
    # missing status is itself a harness bug, not a pass
    status = result.get("status", "internal_error")
    if status == "pass":
        # Per-test granularity: a passing test that skipped `mode skip` region(s) surfaces the
        # count as a warning (pytest has no native "passed-with-skips" outcome).
        skipped = (result.get("stats") or {}).get("skip-mode", 0)
        if skipped:
            warnings.warn(SqlLogicSkipWarning(f"{skipped} statement(s) skipped (mode skip)"))
        return
    if status == "skip":
        pytest.skip(result.get("reason", "skipped"))
    if status == "fail":
        # A SQL-body assertion failure is a TEST failure, not a harness bug: surface it
        # cleanly (the `.test:NN` location, NO Python traceback) via pytrace=False. This keeps
        # the invariant that a Python stack means ONLY a pytest/driver/provisioner failure --
        # a bare `foo.test:NN` is always "the SQL body's assertion failed".
        pytest.fail(result.get("output", ""), pytrace=False)
    # internal_error (e.g. test absent from batch results) or any unexpected status IS a
    # harness bug: raise a real exception (Python traceback) so infra failures stay loud.
    raise SqlLogicFailure(result.get("output", f"internal error: unexpected status {status!r}"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SqlLogicFailure(Exception):
    pass


class SqlLogicSkipWarning(UserWarning):
    pass
