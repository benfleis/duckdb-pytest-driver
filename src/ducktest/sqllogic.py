"""The SQLLogic `.test` lane: collect & run `.test` files through the unittest binary.

One `.test` file -> one SqlLogicItem; the item invokes the Catch2 unittest binary with the
test path as a name filter, batched via `--batch-size` (batch assignment lives in
driver.plugin's pytest_collection_modifyitems). Pass/skip/fail is parsed from the binary's
name-tagged `[TEST_EVENT]` stream (--emit-test-events), so outcomes are attributable per test
even inside a batch. Binary resolution, `run_paired` (driving a same-stem body), and all hooks
live in driver.plugin.
"""

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import threading
import warnings

import pytest


# ---------------------------------------------------------------------------
# <batch-id>: the per-invocation temp run-root segment (SPEC §11.2 / §11.4).
# The composed --temp-dir-base is <root>/<session-id>/<batch-id>; <batch-id>
# identifies ONE unittest invocation so concurrent invocations never share a
# run-root (§11.6 concurrency). A batched invocation reuses the batch's id; a
# single (unbatched) test / paired-driver invocation derives one from the test
# name; an individual re-run gets its OWN distinct id (see _rerun_batch_id).
# ---------------------------------------------------------------------------


def _batch_dir(batch_no) -> str:
    return f"batch-{batch_no}"


def _test_batch_id(test_name: str) -> str:
    return "test-" + hashlib.sha1(test_name.encode()).hexdigest()[:10]


def item_batch_id(item) -> str:
    """The `<batch-id>` path segment for an item's temp run-root (SPEC §11.2).

    A batched `SqlLogicItem` carries its `_batch_id` (assigned by `assign_batches`); an unbatched
    single test (`--batch-size 1`) has none, so its id derives from the test name. The controller
    computes the SAME id for its keep-list mapping (node-id → batch-id), so both agree by construction.
    """
    bid = getattr(item, "_batch_id", None)
    return _batch_dir(bid) if bid is not None else _test_batch_id(item._test_name)


def _rerun_batch_id(batch_id: str, test_name: str) -> str:
    """A DISTINCT batch-id for an individual re-run so its run-root never collides with the batch's
    (SPEC §11.6). The re-run is a throwaway for a clean per-test diff — the failed test's authoritative
    artifacts already live under the batch's own `<batch-id>/<test-id>/`, which the keep-list preserves."""
    return f"{batch_id}-rerun-{_test_batch_id(test_name)}"


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
        from .plugin import _temp_roots  # lazy: plugin imports this lane (avoid import cycle)

        test_name = os.path.relpath(str(self.path), self._working_dir)
        yield SqlLogicItem.from_parent(
            self,
            name=self.path.stem,
            test_name=test_name,
            binary=self._binary,
            working_dir=self._working_dir,
            temp_roots=_temp_roots(self.config),
        )


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class SqlLogicItem(pytest.Item):
    @classmethod
    def from_parent(cls, parent, *, test_name, binary, working_dir, temp_roots=None, **kwargs):
        obj = super().from_parent(parent, **kwargs)
        obj._test_name = test_name
        obj._binary = binary
        obj._working_dir = working_dir
        obj._temp_roots = temp_roots
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
            self._temp_roots,
            batch_id=item_batch_id(self),
            extra_args=resolve_unittest_args(self.config),
        )
        _raise_for_result(_parse_result(result), test_file=str(self.path))

    # -- batch path (batch_size > 1) -----------------------------------------

    def _run_batched(self):
        with _batch_cache_lock:
            if self._batch_id not in _batch_cache:
                _batch_cache[self._batch_id] = _execute_batch(
                    self._batch_test_names,
                    self._binary,
                    self._working_dir,
                    self._temp_roots,
                    batch_id=item_batch_id(self),
                    extra_args=resolve_unittest_args(self.config),
                )
        r = _batch_cache[self._batch_id].get(self._test_name, {"status": "internal_error"})
        _raise_for_result(r, test_file=str(self.path))

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
    test_names: list,
    binary: str,
    working_dir: str,
    temp_roots: dict = None,
    batch_id: str = None,
    extra_args: list = None,
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
            temp_roots,
            batch_id=batch_id,
            extra_args=extra_args,
        )
    finally:
        os.unlink(tmpfile)

    # Per-test outcome is attributable from the name-tagged [TEST_EVENT] stream, so a clean batch
    # needs no re-run. A test the events mark failed/incomplete is re-run alone to get a clean,
    # per-test diff (the batch's combined output isn't sliced per test). The re-run runs under its
    # OWN distinct batch-id (SPEC §11.6) so its run-root never collides with the batch's.
    events = _scan_test_events(result["stdout"] + result["stderr"])
    statuses = {}
    for name in test_names:
        st = _classify(events, name, result)
        if st["status"] in ("fail", "internal_error"):
            st = _invoke_single(
                name, binary, working_dir, temp_roots, batch_id=_rerun_batch_id(batch_id, name), extra_args=extra_args
            )
        statuses[name] = st
    return statuses


def _invoke_single(
    test_name: str,
    binary: str,
    working_dir: str,
    temp_roots: dict = None,
    batch_id: str = None,
    extra_args: list = None,
) -> dict:
    result = _invoke(binary, [test_name], working_dir, temp_roots, batch_id=batch_id, extra_args=extra_args)
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


def _compose_base(root, session_id, batch_id) -> str:
    """The ONE composed per-batch run-root string `<root>/<session-id>/<batch-id>` (SPEC §11.4).

    Joined with `/` so it is correct for a local root (POSIX) AND a remote one (`s3://…`); the binary
    then appends only the `<test-id>` leaf under it.
    """
    return "/".join((str(root).rstrip("/"), str(session_id), str(batch_id)))


def _invoke(
    binary: str,
    args: list,
    working_dir: str,
    temp_roots: dict = None,
    batch_id: str = None,
    env: dict = None,
    extra_args: list = None,
) -> dict:
    # Opt into the binary's per-test event stream on every invocation (single + batch). Requires the
    # C++ --emit-test-events flag to be built into the binary, else Catch2 errors on the unknown arg.
    # extra_args (the --unittest-args passthrough) follow, before the test name / -f selectors.
    args = ["--emit-test-events", *(extra_args or []), *args]
    run_env = dict(os.environ)
    if temp_roots:
        # SPEC §11.4: compose the per-batch run-root and pass it as the ONE --temp-dir-base string
        # (<root>/<session-id>/<batch-id>) — NOT --temp-dir EXACT, and NOT a TEMP_DIR/LOCAL_*/DATA_DIR
        # env var (the binary composes/derives + would overwrite those). --temp-dir-run-id off so the
        # binary appends no extra run-id level (ResolveRunIdRoot returns the base verbatim). The binary
        # then adds only the <test-id> leaf, derives LOCAL_*, and owns local create/sweep. --temp-dir-
        # destroy is passed THROUGH to gate the binary's LOCAL sweep. DATA is a plain read-only path:
        # --data-dir only when the driver has an override, else the binary defaults to working_dir/data.
        base = _compose_base(temp_roots["root"], temp_roots["session_id"], batch_id)
        temp_args = ["--temp-dir-base", base, "--temp-dir-run-id", "off"]
        if temp_roots.get("destroy"):
            temp_args += ["--temp-dir-destroy", str(temp_roots["destroy"])]
        if temp_roots.get("data_dir"):
            temp_args += ["--data-dir", str(temp_roots["data_dir"])]
        args = [*temp_args, *args]
    # env (e.g. provisioned UC_TEST_CATALOG/SCHEMA) is merged over the ambient env so
    # the body's ${...} substitution resolves to the provisioned values.
    if env:
        run_env.update(env)
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
            "stderr": f"unittest binary not found: {binary}\nBuild the extension first (e.g. make debug).",
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
            ev = json.loads(line[i + len(_FLARE) :])
        except ValueError:
            continue
        if ev.get("event") == "end":
            tests[ev.get("name")] = ev
    return tests


def _binary_output(combined: str) -> str:
    """The binary's human output with the machine ``[TEST_EVENT]`` flare lines stripped — i.e. the
    "Wrong result / Expected / Actual" diff a failing ``.test`` prints, for surfacing in the pytest
    failure repr. (The events are parsed separately; here they'd just be noise.)"""
    return "\n".join(ln for ln in combined.splitlines() if _FLARE not in ln).strip()


def _classify(events: dict, name, result: dict) -> dict:
    """Resolve one test's pass/skip/fail from its `end` event, falling back to the return code."""
    combined = result["stdout"] + result["stderr"]
    ev = events.get(name)
    if ev is None:
        # No terminal event for this test (e.g. a crash before `end`, or an old binary): trust rc.
        if result.get("returncode", 1) != 0:
            return {"status": "fail", "output": _binary_output(combined) or "test failed (no output captured)"}
        return {"status": "pass", "stats": {}}
    stats = {"passes": ev.get("passes", 0), "fails": ev.get("fails", 0), "skip-mode": ev.get("skip-mode", 0)}
    status = ev.get("status")
    if status == "skip-requirement":
        return {"status": "skip", "reason": ev.get("data") or "skipped", "stats": stats}
    if status == "error":
        # Surface the binary's FULL failure output — the "Wrong result in query! / Expected: / Actual:"
        # block that lives in stdout. The event `data` (when present) is usually just the `.test:NN`
        # location, already in that output — so prefer the rich text, falling back to `data` only when
        # nothing was captured (an infra error with no stdout). (Was: `data or combined`, which dropped
        # the whole diff whenever `data` held a bare location — the "opaque failure" bug.)
        text = _binary_output(combined)
        if not text:
            text = ev.get("data") or "test failed (no output captured)"
        elif ev.get("data") and ev["data"] not in text:
            text = f"{ev['data']}\n\n{text}"
        return {"status": "fail", "output": text, "stats": stats}
    return {"status": "pass", "stats": stats}


def _parse_result(result: dict) -> dict:
    """Classify a single-test invocation from its [TEST_EVENT] stream."""
    events = _scan_test_events(result["stdout"] + result["stderr"])
    name = next(iter(events), None)
    return _classify(events, name, result)


def _raise_for_result(result: dict, *, test_file: str | None = None) -> None:
    # missing status is itself a harness bug, not a pass
    status = result.get("status", "internal_error")
    if status == "pass":
        # Per-test granularity: a passing test that skipped `mode skip` region(s) surfaces the
        # count as a warning (pytest has no native "passed-with-skips" outcome).
        skipped = (result.get("stats") or {}).get("skip-mode", 0)
        if skipped:
            msg = f"{skipped} statement(s) skipped (mode skip)"
            if test_file:
                # Attribute to the .test body (where the mode-skip regions live) via warn_explicit,
                # so the warnings summary points at the test file instead of this internal warn()
                # call site. lineno=1: we have a count, not the skipped lines.
                warnings.warn_explicit(msg, SqlLogicSkipWarning, test_file, 1)
            else:
                warnings.warn(SqlLogicSkipWarning(msg))
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
