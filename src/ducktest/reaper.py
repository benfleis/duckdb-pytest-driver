"""REMOTE TEMP storage reaper — one session-end sweep (SPEC §11.5).

The lifecycle contract splits by *who holds the filesystem*: **local is entirely the binary's job**
— it creates/reaps the local `<session-id>/<batch-id>/<test-id>` tree as it goes (per-test reap +
`ReclaimLevels` empty-prune); the driver does NOTHING local. **Remote** is entirely the driver's job,
applied **once at session completion** — because the driver is the process that holds the object-store
credentials (the rclone `Remote`), and the binary's remote clamp skips create AND reap. A remote reap
is a **prefix delete** (path-addressed, order-independent), which is why SPEC §11.5 deletes the old
per-test / per-run / `reclaim_physical` machinery.

Reaping is scoped to TEMP only — **DATA is never reaped**. The REMOTE TEMP session prefix is
`<root>/<session-id>/`. At `sessionfinish` (controller-only) the driver builds a **keep-list** of the
batches that contain a FAILED test and runs one sweep:

    rclone delete <root>/<session-id>/ --exclude-from <keeplist>   # keeplist lines: <batch-id>/**
    rclone rmdirs <root>/<session-id>/                             # vacuum the now-empty passers

Everything not under a kept batch is deleted; empty `<batch-id>/`/`<session-id>/` ancestors vacuum
away; failed batches survive. Two backstops: an interrupted run that never reaches `sessionfinish`
does no sweep, and the **age-sweep** purges whole `<root>/<old-session-id>` prefixes older than
`--temp-reap-age-days`.

**Keep-list granularity (SPEC §11.6, under-reap bias).** Over-reap — deleting a failed test's
artifacts — is the one forbidden outcome, so the keep-list must be authoritative/broad, never
reconstructed-and-wrong. We keep at the finest granularity the controller knows *reliably*: a failed
test's **batch** (the controller has the failed node-ids from the report stream and the node-id→batch-id
map), keeping `<batch-id>/**` for any batch with a failure. We do NOT reconstruct the binary's per-test
`TEST_ID` leaf (uncertain). If even the batch map isn't cleanly available, we fall back to keeping the
whole `<session-id>/` on any failure (coarsest safe). TODO(follow-up): per-test-leaf keep needs the
binary's `TEST_ID` (or a `.failed-dirs` file it writes) to be under-reap-safe.

The registered reaper is a small object exposing ``sweep(prefix, keep_patterns)`` (delete-with-exclude
+ rmdirs) and optionally ``list_run_prefixes(base) -> [(prefix, session_id)]`` (for the age-sweep) — a
suite/backend builds it from its own storage creds (e.g. an rclone ``Remote`` + the rclone verbs). No
azure/s3 specifics live here: the framework owns *when* + the keep-list; the reaper owns the *how*.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .context import get_context

log = logging.getLogger("driver")


# ---------------------------------------------------------------------------
# Registration seam (keeps the framework backend-agnostic)
# ---------------------------------------------------------------------------


def register_temp_reaper(config, reaper) -> None:
    """Register the object that reaps this run's REMOTE TEMP storage. It must expose
    ``sweep(prefix, keep_patterns)`` — delete everything under ``prefix`` except paths matching a
    keep pattern, then rmdirs the emptied ancestors (e.g. ``rclone delete <prefix> --exclude-from
    <keep_patterns>`` + ``rclone rmdirs <prefix>``) — and optionally ``list_run_prefixes(base)`` for
    the age-sweep. Call from a backend/suite conftest's ``pytest_configure``, building it from that
    backend's storage creds. With none registered, every level below is a no-op."""
    get_context(config).temp_reaper = reaper


def get_temp_reaper(config):
    """The registered reaper for this process, or None (→ the reaper is a no-op)."""
    return getattr(get_context(config), "temp_reaper", None)


# ---------------------------------------------------------------------------
# The remote gate: <root> must be remote (local is the binary's job). Scoped to
# TEMP only — the session prefix is <root>/<session-id>/; DATA is never reaped.
# ---------------------------------------------------------------------------


def _remote_root(config):
    """This run's ``<root>`` if it is REMOTE, else None (a no-op signal — local is the binary's).
    Computed from the driver's originated roots (SPEC §11.2); TEMP only, never DATA."""
    from .plugin import _is_remote_root, _temp_roots

    root = _temp_roots(config)["root"]
    return root if _is_remote_root(root) else None


def _session_prefix(config):
    """This run's REMOTE TEMP session prefix ``<root>/<session-id>/`` (the sweep target), or None when
    ``<root>`` is local. Trailing slash so the keep-list lines (``<batch-id>/**``) address dirs under it."""
    root = _remote_root(config)
    if root is None:
        return None
    from .plugin import _temp_roots

    return f"{root}/{_temp_roots(config)['session_id']}/"


# ---------------------------------------------------------------------------
# Controller-side failed-node collection (SPEC §11.5): NOT a worker-side stash.
# xdist forwards worker reports to the controller, so the controller's
# pytest_runtest_logreport (see controller.py) sees every test's outcome.
# ---------------------------------------------------------------------------


def record_failure(config, report) -> None:
    """Record a failed test's node-id on the controller (from the report stream). Any phase's failure
    counts (a failed setup/call/teardown ⇒ keep the test's artifacts). Passes/skips are ignored."""
    if not getattr(report, "failed", False):
        return
    get_context(config).failed_nodeids.add(report.nodeid)


# ---------------------------------------------------------------------------
# The keep-list: failed node-ids → the batches to spare (SPEC §11.6)
# ---------------------------------------------------------------------------


def _keep_patterns(config) -> list[str]:
    """Build the sweep's keep-list from the failed node-ids + the controller's node-id→batch-id map.

    - No failures → ``[]`` (keep nothing → everything under the session prefix is reaped).
    - A failure whose batch is known → keep ``<batch-id>/**`` for each such batch (finest reliable
      granularity — SPEC §11.6).
    - The batch map missing, or a failed node NOT in it (e.g. a driver ``.py`` that isn't a batched
      `SqlLogicItem`) → keep the WHOLE session (``["**"]``): the coarsest safe, under-reap choice —
      never over-reap a failed test's artifacts.
    """
    ctx = get_context(config)
    failed = ctx.failed_nodeids
    if not failed:
        return []
    plan = ctx.plan
    node_batch = getattr(plan, "node_batch_ids", None) if plan is not None else None
    if not node_batch:
        return ["**"]  # coarsest safe: no reliable map → keep the whole session on any failure
    batches = set()
    for nodeid in failed:
        bid = node_batch.get(nodeid)
        if bid is None:
            return ["**"]  # a failed node we can't map to a batch → keep everything (never over-reap)
        batches.add(bid)
    return sorted(f"{b}/**" for b in batches)


# ---------------------------------------------------------------------------
# The one session-end sweep — controller-only, keep-on-failure
# ---------------------------------------------------------------------------


def sweep_session(config) -> None:
    """At session end (controller): if a reaper is registered and ``<root>`` is remote, sweep the
    session prefix ``<root>/<session-id>/`` once, sparing the batches of failed tests (the keep-list).
    No-op when ``<root>`` is local (the binary owns local) or no reaper is registered. Best-effort: a
    sweep error is logged, never raised (a session-end reap must not turn the run red)."""
    reaper = get_temp_reaper(config)
    if reaper is None:
        return
    prefix = _session_prefix(config)
    if prefix is None:
        return  # local <root>: local is the binary's job, driver does nothing
    keeps = _keep_patterns(config)
    try:
        reaper.sweep(prefix, keeps)
    except Exception as exc:  # noqa: BLE001 — session-end sweep must not raise
        log.warning("remote reap: session sweep of %s failed: %s", prefix, exc)


# ---------------------------------------------------------------------------
# Age-sweep (backstop, best-effort) — controller-side, once
# ---------------------------------------------------------------------------

# session-ids are date-prefixed (mnemonic.py): the form `2026-07-18T…` starts with a parseable Y[-]M[-]D.
_RUN_DATE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


def _parse_run_date(session_id):
    """The UTC date embedded at the head of a session-id, or None if it doesn't parse."""
    m = _RUN_DATE.match(session_id or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def age_sweep(config) -> None:
    """Best-effort backstop (SPEC §11.5): if the reaper supports ``list_run_prefixes(root)``, purge each
    ``<root>/<old-session-id>`` prefix whose date is older than ``--temp-reap-age-days`` (default 7) via
    ``sweep(prefix, [])`` (empty keep-list = full purge). One bad/unparseable prefix is skipped, not
    fatal; a listing or sweep error is logged and the run is never failed. Runs once, controller-only."""
    reaper = get_temp_reaper(config)
    if reaper is None or not hasattr(reaper, "list_run_prefixes"):
        return
    root = _remote_root(config)
    if root is None:
        return
    days = int(config.getoption("--temp-reap-age-days", default=7))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        prefixes = list(reaper.list_run_prefixes(root))
    except Exception as exc:  # noqa: BLE001 — a listing failure never fails the run
        log.warning("remote reap: age-sweep listing of %s failed: %s", root, exc)
        return
    swept = []
    for prefix, session_id in prefixes:
        dt = _parse_run_date(session_id)
        if dt is None or dt >= cutoff:
            continue  # unparseable (skip, don't abort) or fresh (keep)
        try:
            reaper.sweep(prefix, [])  # empty keep-list = full purge + rmdirs of the stale session
            swept.append(prefix)
        except Exception as exc:  # noqa: BLE001 — one sweep failure doesn't abort the age-sweep
            log.warning("remote reap: age-sweep of %s failed: %s", prefix, exc)
    if swept:
        log.info("remote reap: age-sweep purged %d session prefix(es) older than %d day(s)", len(swept), days)
