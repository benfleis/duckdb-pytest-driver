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


def _matrix_cell_properties(item) -> dict:
    """The single per-cell `properties` dict a matrix-fanned `.test` item's cell carries, if any
    (`plugin.py`'s `_expand_test_matrix` stamps `_matrix_cell` with the suite's cell dict verbatim).
    Homogeneous, backend-declared vocabulary -- same spirit as `requires.py`'s `Requirement.properties`:
    a cell says WHAT it needs (`temp_dir_root`, `data_dir`, or any plain env var name); which of
    those becomes a `--temp-dir-base`/`--data-dir` CLI arg vs a literal env var is THIS module's
    call (`_split_matrix_cell_properties`), never the conftest's. `{}` for a non-matrix item or a
    cell with no `properties` key."""
    cell = getattr(item, "_matrix_cell", None)
    return (cell.get("properties") if cell else None) or {}


# Property names this module claims and routes into `temp_roots` (`--temp-dir-base`/`--data-dir`)
# instead of passing through as a literal env var; everything else is a plain env var, verbatim.
# `temp_dir_root` is deliberately NOT `TEMP_DIR`/`--temp-dir-base` itself -- it's the prefix
# `_invoke` composes `<root>/<session-id>/<batch-id>` onto (SPEC §11.2/§11.4). `<batch-id>` isn't
# minted until `assign_batches` runs in the PLAN phase, strictly after a matrix cell's properties
# are fixed at collect+decorate time (`_expand_test_matrix` runs first) -- so a cell can only ever
# declare the root, never the composed TEMP_DIR.
_TEMP_ROOTS_PROPERTY_KEYS = {"temp_dir_root": "root", "data_dir": "data_dir"}

# A cell's `test_config` property is a duckdb `--test-config` JSON path (its `on_init` SQL,
# `statically_loaded_extensions`, `skip_tests`) passed to the binary -- NOT a literal env var.
_TEST_CONFIG_PROPERTY_KEY = "test_config"

# A cell's `init_sql` property is inline preamble SQL (e.g. `SET x='y';`) merged into the item's
# `--init-sqllogic` snippet ahead of the body (plugin._init_sqllogic_arg_for_item) -- NOT an env var.
# The lighter alternative to `test_config` for a pure-SET cell (see docs/MATRIX.md).
_INIT_SQL_PROPERTY_KEY = "init_sql"

# Property names claimed by a dedicated mechanism, so never routed into env vars / temp roots.
_CLAIMED_PROPERTY_KEYS = frozenset({_TEST_CONFIG_PROPERTY_KEY, _INIT_SQL_PROPERTY_KEY})


def _split_matrix_cell_properties(properties: dict):
    """Split a cell's `properties` dict into `(env, temp_roots)` -- the ONE place that decides which
    property names are `--temp-dir-base`/`--data-dir` CLI args (`_TEMP_ROOTS_PROPERTY_KEYS`) vs
    literal env vars (everything else, passed through verbatim). `_CLAIMED_PROPERTY_KEYS`
    (`test_config`/`init_sql`) are handled by their own mechanisms, never env vars."""
    env, temp_roots = {}, {}
    for key, value in properties.items():
        if key in _CLAIMED_PROPERTY_KEYS:
            continue
        dest = _TEMP_ROOTS_PROPERTY_KEYS.get(key)
        (temp_roots if dest else env)[dest or key] = value
    return env, temp_roots


def _matrix_cell_init_sql(item):
    """A matrix cell's `init_sql` property -- inline preamble SQL merged into the item's
    `--init-sqllogic` snippet ahead of the body -- or `None` for a non-matrix item / a cell without
    one. Merged (not a second `--init-sqllogic`) by `plugin._init_sqllogic_arg_for_item`."""
    return _matrix_cell_properties(item).get(_INIT_SQL_PROPERTY_KEY) or None


def _matrix_cell_test_config_args(item, working_dir) -> list:
    """`["--test-config", <path>]` when a matrix cell declares a `test_config` property (a duckdb
    test-config JSON: its `on_init` SQL, `statically_loaded_extensions`, `skip_tests`); `[]` for a
    non-matrix item or a cell without one. Path resolves relative to `working_dir`. Reusing duckdb's
    native `--test-config` lets a consumer matrix over its existing config JSONs (see docs/MATRIX.md)."""
    path = _matrix_cell_properties(item).get(_TEST_CONFIG_PROPERTY_KEY)
    if not path:
        return []
    return ["--test-config", path if os.path.isabs(path) else os.path.join(working_dir, path)]


def _matrix_cell_env(item) -> dict:
    """The per-invocation env override for a matrix-fanned `.test` item, from its cell's
    `properties` (the non-`temp_dir_root`/`data_dir` entries) -- e.g. two cells of the same file
    needing DIFFERENT values can't both mutate the shared `os.environ` (the up-front `to_env`/adopt
    model is invocation-wide, not cell-aware); `_invoke`'s own `env=` already layers per-subprocess-
    call, which is exactly cell-scoped. `None` for a non-matrix item or a cell with no plain-env
    properties (today's behavior, unchanged)."""
    env, _ = _split_matrix_cell_properties(_matrix_cell_properties(item))
    return env or None


def _matrix_cell_temp_roots(item, base: dict) -> dict:
    """`base` (the run-wide `_temp_roots(config)`), with a matrix cell's `temp_dir_root`/`data_dir`
    properties overlaid as `root`/`data_dir` -- same rationale as `_matrix_cell_env`, for the SAME
    two fields (`--temp-dir-base`/`--data-dir` are already remote-capable, SPEC §11.2/§11.3, but
    `_temp_roots` is cached once per `config`, not cell-aware): two cells of the same file needing
    DIFFERENT remote roots (e.g. `az://` vs `abfss://`, different storage accounts) can't share one
    `--temp-dir-base`/`--data-dir`. `session_id`/`destroy` stay run-wide -- only `root`/`data_dir`
    are ever cell-specific. `base` unchanged for a non-matrix item or a cell with neither property."""
    _, override = _split_matrix_cell_properties(_matrix_cell_properties(item))
    if not override:
        return base
    return {**base, **{k: v for k, v in override.items() if v is not None}}


def _cell_suffix(cell) -> str:
    """A stable string suffix for a matrix cell (a bare id string, a cell dict, or None — both
    shapes occur: `.test` fan-out stamps a plain backend-id string, `.py`/`@requires_matrix` cells
    are dicts), folded into a batch-id seed so two cells of the SAME test never collide on the same
    temp-dir path. `""` for None (today's behavior, unchanged for every non-matrix test)."""
    if not cell:
        return ""
    if isinstance(cell, dict):
        cell = ",".join(f"{k}={cell[k]}" for k in sorted(cell))
    return f"[{cell}]"


def item_batch_id(item) -> str:
    """The `<batch-id>` path segment for an item's temp run-root (SPEC §11.2).

    A batched `SqlLogicItem` carries its `_batch_id` (assigned by `assign_batches`); an unbatched
    single test (`--batch-size 1`) has none, so its id derives from the test name (+ its matrix
    `_cell` stamp, if any — two cell siblings of the same file share a `_test_name` and, unbatched,
    would otherwise collide on the identical temp-dir path). The controller computes the SAME id
    for its keep-list mapping (node-id → batch-id) by calling this function directly, so both agree
    by construction.
    """
    bid = getattr(item, "_batch_id", None)
    if bid is not None:
        return _batch_dir(bid)
    return _test_batch_id(item._test_name + _cell_suffix(getattr(item, "_cell", None)))


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


def _new_item(parent, *, name, test_name, binary, working_dir, temp_roots):
    """The one place that builds a `SqlLogicItem` from its invocation-constant fields — shared by
    `SqlLogicFile.collect()` (the original, one per file) and the suite-matrix `.test` splice
    (`plugin.py`'s `_expand_test_matrix`, one sibling per cell) so a `from_parent` signature change
    has exactly one call site to update, not two independently-drifting ones."""
    return SqlLogicItem.from_parent(
        parent, name=name, test_name=test_name, binary=binary, working_dir=working_dir, temp_roots=temp_roots
    )


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
        yield _new_item(
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
        from .plugin import _init_sqllogic_arg_for_item

        result = _invoke(
            self._binary,
            [self._test_name],
            self._working_dir,
            _matrix_cell_temp_roots(self, self._temp_roots),
            batch_id=item_batch_id(self),
            env=_matrix_cell_env(self),
            extra_args=[
                *_matrix_cell_test_config_args(self, self._working_dir),
                *_init_sqllogic_arg_for_item(self.config, self),
                *resolve_unittest_args(self.config),
            ],
        )
        _raise_for_result(_parse_result(result), test_file=str(self.path))

    # -- batch path (batch_size > 1) -----------------------------------------

    def _run_batched(self):
        with _batch_cache_lock:
            if self._batch_id not in _batch_cache:
                from .plugin import _init_sqllogic_arg_for_item

                _batch_cache[self._batch_id] = _execute_batch(
                    self._batch_test_names,
                    self._binary,
                    self._working_dir,
                    _matrix_cell_temp_roots(self, self._temp_roots),
                    batch_id=item_batch_id(self),
                    env=_matrix_cell_env(self),
                    extra_args=[
                        *_matrix_cell_test_config_args(self, self._working_dir),
                        *_init_sqllogic_arg_for_item(self.config, self),
                        *resolve_unittest_args(self.config),
                    ],
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
    env: dict = None,
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
            env=env,
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
                name,
                binary,
                working_dir,
                temp_roots,
                batch_id=_rerun_batch_id(batch_id, name),
                env=env,
                extra_args=extra_args,
            )
        statuses[name] = st
    return statuses


def _invoke_single(
    test_name: str,
    binary: str,
    working_dir: str,
    temp_roots: dict = None,
    batch_id: str = None,
    env: dict = None,
    extra_args: list = None,
) -> dict:
    result = _invoke(binary, [test_name], working_dir, temp_roots, batch_id=batch_id, env=env, extra_args=extra_args)
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
        # TODO: DuckDB's --temp-dir-base has no DUCKDB_TEST_*-style env fallback (unlike --data-dir),
        # and "base" reads worse than "root" for what's really a prefix another two levels get
        # appended onto (SPEC §11.4) -- once upstream lands an env-settable, root-named equivalent,
        # switch this (and the CLI flag this driver itself exposes) over to match.
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
    result = {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="replace"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
    }
    if temp_roots:
        # The event-tag check (RESOURCE-PLANNING.md §5 phase 9): confirm the binary's [TEST_EVENT]
        # stream is actually attributable to THIS invocation, not just assumed to be from "whatever
        # subprocess I launched" -- `base` is the one value we know this invocation was given.
        _verify_event_temp_dirs(_scan_test_events(result["stdout"] + result["stderr"]), base)
    return result


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


def _verify_event_temp_dirs(events: dict, expected_base: str) -> None:
    """Fail loud if any `end` event's echoed `temp_dir` doesn't start with THIS invocation's
    composed `--temp-dir-base` (the event-tag check, RESOURCE-PLANNING.md §5 phase 9) — proof the
    stream belongs to the subprocess we just ran, not silently assumed. A binary too old to echo
    `temp_dir` (the field is simply absent) skips the check for that event, same as `_classify`'s
    existing "no terminal event -> trust the return code" fallback for an old/crashed binary.
    """
    for name, ev in events.items():
        temp_dir = ev.get("temp_dir")
        if temp_dir and not temp_dir.startswith(expected_base):
            raise RuntimeError(
                f"[TEST_EVENT] for {name!r} echoed temp_dir={temp_dir!r}, which does not start with "
                f"this invocation's --temp-dir-base={expected_base!r} -- the event stream may be "
                "misattributed to the wrong subprocess."
            )


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
