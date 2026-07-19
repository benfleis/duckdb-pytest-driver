"""Self-tests for the nested REMOTE storage reaper (SPEC §11.4) — offline, mock reaper.

The driver reaps the REMOTE TEMP run-root ``$BASE/$RUN_ID`` at three nested levels (per-test / per-run /
age-sweep), each keep-on-failure at its level, each a no-op when ``$BASE`` is local (the binary owns
local) or when no reaper is registered. Remoteness comes from ``--temp-dir-base`` (the driver's $BASE),
NOT a driver-set ``TEMP_DIR`` env var, and reaping is scoped to TEMP only — DATA is never reaped. These
exercise the framework's *when* + keep-on-failure gating against a FAKE reaper that only records its
``purge``/``list_run_prefixes`` calls — no rclone / object store needed.

What still needs a REAL rclone/object-store to validate: that the recorded prefixes actually map to and
delete the right remote objects (the reaper's *how*) — and that the per-test token subpath lines up with
the binary's ``$TEST_ID`` leaf for a remote base. That's the backend's job, not the framework's.
"""

import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from ducktest import register_temp_reaper
from ducktest.context import SessionContext, set_context
from ducktest.plugin import _provision_token
from ducktest.reaper import (
    REP_CALL_KEY,
    REP_SETUP_KEY,
    age_sweep,
    reap_run,
    reap_test,
    stash_report,
)

RUN_ID = "2026-07-18T00-00-00Z--brave-fox-42"
REMOTE = "s3://bucket/scratch"  # a remote --temp-dir-base
_ROOT_VARS = ("TEMP_DIR", "LOCAL_TEMP_DIR", "DATA_DIR", "LOCAL_DATA_DIR")


def _run_root():
    """The REMOTE TEMP run-root the binary composes as TEMP_DIR: $BASE/$RUN_ID."""
    return f"{REMOTE}/{RUN_ID}"


# -----------------------------------------------------------------------------
# Fakes
#


class FakeReaper:
    """Records purge calls; no real object store."""

    def __init__(self):
        self.purged = []

    def purge(self, prefix):
        self.purged.append(prefix)


class FakeSweepReaper(FakeReaper):
    """A reaper that also lists run prefixes (drives the age-sweep). A prefix ending in ``explode``
    raises on purge — to prove one bad prefix doesn't abort the sweep."""

    def __init__(self, listing):
        super().__init__()
        self._listing = listing

    def list_run_prefixes(self, base):
        return list(self._listing)

    def purge(self, prefix):
        if prefix.endswith("explode"):
            raise RuntimeError("purge blew up")
        super().purge(prefix)


class Rep:
    def __init__(self, when, outcome):
        self.when = when
        self.outcome = outcome


class Item:
    def __init__(self, nodeid, config):
        self.nodeid = nodeid
        self.config = config
        self.stash = pytest.Stash()


class Session:
    def __init__(self, testsfailed):
        self.testsfailed = testsfailed


class Cfg:
    """Minimal pytest.Config stand-in: a typed stash (for the SessionContext), a fixed run-id, and the
    options the reaper + `_temp_roots` read. ``temp_dir_base`` is the $BASE (remote → the reaper fires;
    local → it no-ops)."""

    def __init__(self, destroy="on-success", age_days=7, temp_dir_base="/tmp/base"):
        self.stash = pytest.Stash()
        self._sqllogic_run_id = RUN_ID
        self._opts = {
            "--temp-dir-base": temp_dir_base,
            "--data-dir": None,
            "--temp-dir-destroy": destroy,
            "--temp-reap-age-days": age_days,
        }

    def getoption(self, name, default=None):
        return self._opts.get(name, default)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ROOT_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _config(reaper=None, **kw):
    cfg = Cfg(**kw)
    set_context(cfg, SessionContext(working_dir="/w", run_id=RUN_ID))
    if reaper is not None:
        register_temp_reaper(cfg, reaper)
    return cfg


def _passed(item):
    stash_report(item, Rep("setup", "passed"))
    stash_report(item, Rep("call", "passed"))


# -----------------------------------------------------------------------------
# Registration seam
#


def test_register_temp_reaper_stores_on_context(clean_env):
    from ducktest import get_temp_reaper

    cfg = _config()
    assert get_temp_reaper(cfg) is None  # none registered => no-op
    reaper = FakeReaper()
    register_temp_reaper(cfg, reaper)
    assert get_temp_reaper(cfg) is reaper


# -----------------------------------------------------------------------------
# 1. Per-test reap (primary) — keep-on-failure, remote-only, reaper-gated
#


def test_per_test_pass_purges_token_subpath(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, temp_dir_base=REMOTE)
    item = Item("test/x.py::test_a", cfg)
    _passed(item)
    reap_test(cfg, item)
    assert reaper.purged == [f"{_run_root()}/{_provision_token(cfg, item)}"]


def test_per_test_setup_failure_keeps_artifacts(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, temp_dir_base=REMOTE)
    item = Item("test/x.py::test_a", cfg)
    stash_report(item, Rep("setup", "failed"))  # setup failed → keep-on-failure
    reap_test(cfg, item)
    assert reaper.purged == []


def test_per_test_call_failure_keeps_artifacts(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, temp_dir_base=REMOTE)
    item = Item("test/x.py::test_a", cfg)
    stash_report(item, Rep("setup", "passed"))
    stash_report(item, Rep("call", "failed"))  # call failed → keep-on-failure
    reap_test(cfg, item)
    assert reaper.purged == []


def test_per_test_local_base_is_noop(clean_env):
    # A local $BASE => local is the binary's job, driver no-ops.
    reaper = FakeReaper()
    cfg = _config(reaper, temp_dir_base="/tmp/base")
    item = Item("test/x.py::test_a", cfg)
    _passed(item)
    reap_test(cfg, item)
    assert reaper.purged == []


def test_per_test_no_reaper_is_noop(clean_env):
    cfg = _config(reaper=None, temp_dir_base=REMOTE)  # remote, passed, but nothing registered
    item = Item("test/x.py::test_a", cfg)
    _passed(item)
    reap_test(cfg, item)  # must not raise


def test_makereport_stash_drives_the_gate(clean_env):
    # The gate reads exactly the reports stash_report (the makereport hookwrapper) writes to item.stash.
    reaper = FakeReaper()
    cfg = _config(reaper, temp_dir_base=REMOTE)
    item = Item("test/x.py::test_a", cfg)
    # Nothing stashed yet → not a pass → no purge.
    reap_test(cfg, item)
    assert reaper.purged == []
    # Stash a passing setup+call via the same entry point the hookwrapper uses → now it reaps.
    stash_report(item, Rep("setup", "passed"))
    stash_report(item, Rep("call", "passed"))
    assert item.stash[REP_SETUP_KEY].outcome == "passed"
    assert item.stash[REP_CALL_KEY].outcome == "passed"
    reap_test(cfg, item)
    assert reaper.purged == [f"{_run_root()}/{_provision_token(cfg, item)}"]


# -----------------------------------------------------------------------------
# 2. Per-run reap (safety net) — --temp-dir-destroy disposition, run-root = $BASE/$RUN_ID
#


def test_per_run_on_success_all_passed_purges(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, destroy="on-success", temp_dir_base=REMOTE)
    reap_run(cfg, Session(testsfailed=0))
    assert reaper.purged == [_run_root()]


def test_per_run_on_success_with_failure_keeps(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, destroy="on-success", temp_dir_base=REMOTE)
    reap_run(cfg, Session(testsfailed=1))  # a failed test → retain the run's remnants
    assert reaper.purged == []


def test_per_run_always_purges_regardless(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, destroy="always", temp_dir_base=REMOTE)
    reap_run(cfg, Session(testsfailed=3))
    assert reaper.purged == [_run_root()]


def test_per_run_never_skips(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, destroy="never", temp_dir_base=REMOTE)
    reap_run(cfg, Session(testsfailed=0))
    assert reaper.purged == []


def test_per_run_local_base_is_noop(clean_env):
    reaper = FakeReaper()
    cfg = _config(reaper, destroy="always", temp_dir_base="/tmp/base")
    reap_run(cfg, Session(testsfailed=0))
    assert reaper.purged == []


# -----------------------------------------------------------------------------
# 3. Age-sweep (backstop, best-effort) — lists under $BASE
#


def _runid(days_ago):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return f"{d.strftime('%Y%m%d')}_stale_mnem"


def test_age_sweep_purges_only_stale_prefixes(clean_env):
    old, fresh = _runid(10), _runid(1)
    listing = [
        (f"{REMOTE}/{old}", old),
        (f"{REMOTE}/{fresh}", fresh),
    ]
    reaper = FakeSweepReaper(listing)
    cfg = _config(reaper, age_days=7, temp_dir_base=REMOTE)
    age_sweep(cfg)
    assert reaper.purged == [f"{REMOTE}/{old}"]  # only the >7d prefix; the fresh one is kept


def test_age_sweep_tolerates_bad_prefix_and_purge_error(clean_env):
    old = _runid(30)
    listing = [
        (f"{REMOTE}/{old}", old),  # stale → purged
        (f"{REMOTE}/garbage", "garbage"),  # unparseable run-id → skipped, not fatal
        (f"{REMOTE}/{_runid(40)}explode", _runid(40)),  # stale but purge raises → doesn't abort sweep
    ]
    reaper = FakeSweepReaper(listing)
    cfg = _config(reaper, age_days=7, temp_dir_base=REMOTE)
    age_sweep(cfg)  # must not raise
    assert reaper.purged == [f"{REMOTE}/{old}"]  # the good stale one still got swept


def test_age_sweep_noop_without_list_support(clean_env):
    # A reaper without list_run_prefixes → the age-sweep is silently skipped (per-test/per-run still work).
    reaper = FakeReaper()
    cfg = _config(reaper, temp_dir_base=REMOTE)
    age_sweep(cfg)
    assert reaper.purged == []


def test_age_sweep_local_base_is_noop(clean_env):
    reaper = FakeSweepReaper([(f"{REMOTE}/{_runid(99)}", _runid(99))])
    cfg = _config(reaper, temp_dir_base="/tmp/base")
    age_sweep(cfg)
    assert reaper.purged == []


# -----------------------------------------------------------------------------
# End-to-end: the makereport hookwrapper + per-test teardown hook actually fire and reap
#


def test_per_test_reap_wired_through_real_pytest(pytester, monkeypatch):
    """Prove the wiring (makereport stash → pytest_runtest_teardown → reap_test) under a real run: a
    conftest registers a reaper that logs purge calls; one test passes, one fails. Remoteness comes from
    a remote --temp-dir-base. Only the passer's prefix is purged; the failed run means the per-run net
    does not fire (on-success)."""
    reap_log = pytester.path / "reap.log"
    monkeypatch.setenv("REAP_LOG", str(reap_log))
    pytester.makeconftest(
        textwrap.dedent(
            """
            import os
            from ducktest import register_temp_reaper

            class LogReaper:
                def purge(self, prefix):
                    with open(os.environ["REAP_LOG"], "a") as f:
                        f.write(prefix + "\\n")

            def pytest_configure(config):
                register_temp_reaper(config, LogReaper())
            """
        )
    )
    pytester.makepyfile(
        test_inner="""
        def test_pass():
            assert True

        def test_fail():
            assert False
        """
    )
    result = pytester.runpytest_subprocess(
        "-n", "0", "-p", "no:cacheprovider", "--temp-dir-base", REMOTE
    )
    result.assert_outcomes(passed=1, failed=1)
    lines = [ln for ln in reap_log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1  # only the PASSED test reaped; the failed one kept (keep-on-failure)
    assert lines[0].startswith(REMOTE + "/")  # the passer's $BASE/$RUN_ID/${token}
