"""Nested REMOTE storage reaper — the deferred lifecycle piece (SPEC §11.4).

The lifecycle contract splits by *who holds the filesystem*: **unittest** owns LOCAL create + reap
(the existing `RemoveDirectory` path, kept as-is), the **driver** owns REMOTE reaping — because the
driver is the process that holds the object-store credentials (the rclone `Remote`). Remote reaping is
**path-addressed** (a prefix purge), so it is order-independent — which is exactly why SPEC §11.4
deletes `reclaim_physical` + the LIFO teardown ordering.

Three nested levels, each keep-on-failure at its level, each a no-op when `TEMP_DIR` is local (local is
unittest's job) OR when no reaper is registered (the framework is backend-agnostic — it owns *when* and
the keep-on-failure gating; a registered reaper owns *how*, the actual object-store purge/list):

  1. **per-test** (primary): after each test that PASSED, purge ``${TEMP_DIR}/${token}``. Keep-on-failure
     skips the reap if the test's setup OR call failed (its remnants are preserved for debugging). The
     outcome is read from reports the ``pytest_runtest_makereport`` hookwrapper stashes on the item; it
     runs on the worker (which knows its own outcome — no manager queue).
  2. **per-run** (safety net, controller/session end): purge ``${TEMP_DIR}/${run-mnemonic}``, gated by
     the existing ``--temp-dir-destroy`` disposition (never | on-success = only if nothing failed |
     always). Passers already reaped themselves per-test, so on a failed run this retains exactly the
     failed tests' remnants.
  3. **age-sweep** (backstop, best-effort): if the reaper exposes ``list_run_prefixes(base)``, enumerate
     the base prefix, parse each run-id's date (mnemonic run-ids are date-prefixed — see mnemonic.py),
     and purge those older than ``--temp-reap-age-days`` (default 7). Never fails the run on a sweep
     error; logs what it swept.

The registered reaper is a small object exposing ``purge(prefix)`` and optionally
``list_run_prefixes(base) -> [(prefix, mnemonic_or_runid)]`` — a suite/backend builds it from its own
storage creds (e.g. an rclone ``Remote`` + the purge/list verbs). No azure/s3 specifics live here.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from .context import get_context

log = logging.getLogger("driver")

# Reports the makereport hookwrapper stashes per item; the per-test gate reads them.
REP_SETUP_KEY: "pytest.StashKey" = pytest.StashKey()
REP_CALL_KEY: "pytest.StashKey" = pytest.StashKey()


# ---------------------------------------------------------------------------
# Registration seam (keeps the framework backend-agnostic)
# ---------------------------------------------------------------------------


def register_temp_reaper(config, reaper) -> None:
    """Register the object exposing ``purge(prefix)`` (and optionally ``list_run_prefixes(base)``) that
    reaps this run's REMOTE ``TEMP_DIR``. Call from a backend/suite conftest's ``pytest_configure``,
    building it from that backend's storage creds. Registered per process (controller AND each xdist
    worker each run ``pytest_configure`` → each holds its own reaper), so the per-test reap on a worker
    and the per-run reap on the controller both find one — no broadcast needed. With none registered,
    every level below is a no-op."""
    get_context(config).temp_reaper = reaper


def get_temp_reaper(config):
    """The registered reaper for this process, or None (→ the reaper is a no-op)."""
    return getattr(get_context(config), "temp_reaper", None)


# ---------------------------------------------------------------------------
# Per-test outcome stash (standard `rep_setup`/`rep_call` pattern)
# ---------------------------------------------------------------------------


def stash_report(item, rep) -> None:
    """Stash a test report on the item by phase (called from the makereport hookwrapper)."""
    if rep.when == "setup":
        item.stash[REP_SETUP_KEY] = rep
    elif rep.when == "call":
        item.stash[REP_CALL_KEY] = rep


def _test_passed(item) -> bool:
    """True iff setup AND call both ran and passed. Keep-on-failure: a failed setup or call (and a
    test whose call never ran — skip/error) is NOT a pass, so its artifacts are preserved."""
    setup = item.stash.get(REP_SETUP_KEY, None)
    call = item.stash.get(REP_CALL_KEY, None)
    if setup is not None and setup.outcome != "passed":
        return False
    return call is not None and call.outcome == "passed"


# ---------------------------------------------------------------------------
# The remote gate: TEMP_DIR must be remote (local is unittest's job)
# ---------------------------------------------------------------------------


def _remote_temp_dir(config):
    """This run's ``TEMP_DIR`` if it is REMOTE, else None (a no-op signal — local is unittest's)."""
    from .plugin import _is_remote_root, _temp_roots

    temp_dir = _temp_roots(config)["TEMP_DIR"]
    return temp_dir if _is_remote_root(temp_dir) else None


def _run_mnemonic(config):
    """The run-scoped path token ``<YYYYMMDD>_<mnemonic>`` — the per-run prefix; date-prefixed so the
    age-sweep can parse it. Per-test tokens share this prefix (they append a nodeid hash)."""
    from .plugin import _provision_token

    return _provision_token(config)


# ---------------------------------------------------------------------------
# 1. Per-test reap (primary) — worker-side, keep-on-failure
# ---------------------------------------------------------------------------


def reap_test(config, item) -> None:
    """After a test: if TEMP_DIR is remote + a reaper is registered + the test PASSED, purge
    ``${TEMP_DIR}/${token}``. No-op otherwise (local / no reaper / setup-or-call failed). Best-effort:
    a purge error is logged, never raised (a reap must not turn a passed test red)."""
    reaper = get_temp_reaper(config)
    if reaper is None:
        return
    temp_dir = _remote_temp_dir(config)
    if temp_dir is None:
        return
    if not _test_passed(item):
        return  # keep-on-failure: preserve the failed test's artifacts
    from .plugin import _provision_token

    prefix = f"{temp_dir}/{_provision_token(config, item)}"
    try:
        reaper.purge(prefix)
    except Exception as exc:  # noqa: BLE001 — reaping must not fail the (passed) test
        log.warning("remote reap: per-test purge of %s failed: %s", prefix, exc)


# ---------------------------------------------------------------------------
# 2. Per-run reap (safety net) — controller-side, --temp-dir-destroy disposition
# ---------------------------------------------------------------------------


def reap_run(config, session) -> None:
    """At session end (controller): purge ``${TEMP_DIR}/${run-mnemonic}`` per the ``--temp-dir-destroy``
    disposition (reused so remote matches local semantics): never = skip; on-success = only when nothing
    failed; always = unconditional. Passers already reaped per-test, so on a failed on-success run this
    retains exactly the failed tests' remnants. Best-effort: logged, never raised."""
    reaper = get_temp_reaper(config)
    if reaper is None:
        return
    temp_dir = _remote_temp_dir(config)
    if temp_dir is None:
        return
    destroy = config.getoption("--temp-dir-destroy", default="on-success")
    if destroy == "never":
        return
    if destroy == "on-success" and int(session.testsfailed) != 0:
        return  # keep the failed run's remnants
    prefix = f"{temp_dir}/{_run_mnemonic(config)}"
    try:
        reaper.purge(prefix)
    except Exception as exc:  # noqa: BLE001 — session-end reap must not raise
        log.warning("remote reap: per-run purge of %s failed: %s", prefix, exc)


# ---------------------------------------------------------------------------
# 3. Age-sweep (backstop, best-effort) — controller-side, once
# ---------------------------------------------------------------------------

# Run-ids are date-prefixed (mnemonic.py): the run-id form `2026-07-18T…` and the token form
# `20260718_…` both start with a parseable Y[-]M[-]D.
_RUN_DATE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


def _parse_run_date(runid):
    """The UTC date embedded at the head of a run-id/token, or None if it doesn't parse."""
    m = _RUN_DATE.match(runid or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def age_sweep(config) -> None:
    """Best-effort backstop: if the reaper supports ``list_run_prefixes(base)``, purge run prefixes whose
    date is older than ``--temp-reap-age-days`` (default 7). One bad/unparseable prefix is skipped, not
    fatal; a listing or purge error is logged and the run is never failed. Runs once, controller-only."""
    reaper = get_temp_reaper(config)
    if reaper is None or not hasattr(reaper, "list_run_prefixes"):
        return
    temp_dir = _remote_temp_dir(config)
    if temp_dir is None:
        return
    days = int(config.getoption("--temp-reap-age-days", default=7))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        prefixes = list(reaper.list_run_prefixes(temp_dir))
    except Exception as exc:  # noqa: BLE001 — a listing failure never fails the run
        log.warning("remote reap: age-sweep listing of %s failed: %s", temp_dir, exc)
        return
    swept = []
    for prefix, runid in prefixes:
        dt = _parse_run_date(runid)
        if dt is None or dt >= cutoff:
            continue  # unparseable (skip, don't abort) or fresh (keep)
        try:
            reaper.purge(prefix)
            swept.append(prefix)
        except Exception as exc:  # noqa: BLE001 — one purge failure doesn't abort the sweep
            log.warning("remote reap: age-sweep purge of %s failed: %s", prefix, exc)
    if swept:
        log.info("remote reap: age-sweep purged %d run prefix(es) older than %d day(s)", len(swept), days)
