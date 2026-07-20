"""Self-tests for the REMOTE TEMP sweeper — one session-end sweep (SPEC §11.5) — offline, fake sweeper.

The driver sweeps REMOTE TEMP once at ``sessionfinish`` (controller-only): it sweeps the session prefix
``<root>/<session-id>/``, sparing the batches of FAILED tests (the keep-list). Remoteness comes from
``--temp-dir-base`` (the driver's ``<root>``), NOT a driver-set env var, and sweeping is scoped to TEMP
only — DATA is never swept. These exercise the framework's *when* + keep-list against a FAKE sweeper
that only records its ``sweep``/``list_run_prefixes`` calls — no rclone / object store needed.

What still needs a REAL rclone/object-store to validate: that a recorded ``sweep(prefix, keeps)`` maps
to and deletes the right remote objects (``rclone delete <prefix> --exclude-from <keeps>`` + ``rclone
rmdirs``) — the sweeper's *how*. And the per-test-leaf keep-list refinement (keep only a failed
``<batch>/<test-id>/`` instead of the whole ``<batch>/``) needs the binary's ``TEST_ID`` (or a
``.failed-dirs`` file it writes) to stay under-sweep-safe — a follow-up, not the framework's job here.
"""

import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from ducktest import register_temp_sweeper
from ducktest.context import Plan, SessionContext, set_context
from ducktest.sweeper import age_sweep, record_failure, sweep_session

RUN_ID = "2026-07-18T00-00-00Z--brave-fox-42"
REMOTE = "s3://bucket/scratch"  # a remote --temp-dir-base (the <root>)
_ROOT_VARS = ("TEMP_DIR", "LOCAL_TEMP_DIR", "DATA_DIR", "LOCAL_DATA_DIR")


def _session_prefix():
    """The REMOTE TEMP session prefix the driver sweeps: <root>/<session-id>/."""
    return f"{REMOTE}/{RUN_ID}/"


# -----------------------------------------------------------------------------
# Fakes
#


class FakeSweeper:
    """Records sweep calls; no real object store."""

    def __init__(self):
        self.swept = []

    def sweep(self, prefix, keep_patterns):
        self.swept.append((prefix, list(keep_patterns)))


class FakeSweepSweeper(FakeSweeper):
    """A sweeper that also lists session prefixes (drives the age-sweep). A prefix ending in ``explode``
    raises on sweep — to prove one bad prefix doesn't abort the age-sweep."""

    def __init__(self, listing):
        super().__init__()
        self._listing = listing

    def list_run_prefixes(self, base):
        return list(self._listing)

    def sweep(self, prefix, keep_patterns):
        if prefix.endswith("explode"):
            raise RuntimeError("sweep blew up")
        super().sweep(prefix, keep_patterns)


class Rep:
    """Minimal test-report stand-in: what `record_failure` reads (a phase's report)."""

    def __init__(self, nodeid, failed):
        self.nodeid = nodeid
        self.failed = failed


class Cfg:
    """Minimal pytest.Config stand-in: a typed stash (for the SessionContext), a fixed session-id, and
    the options the sweeper reads. ``temp_dir_base`` is the ``<root>`` (remote → the sweeper fires; local
    → it no-ops)."""

    def __init__(self, age_days=7, temp_dir_base=REMOTE):
        self.stash = pytest.Stash()
        self._sqllogic_run_id = RUN_ID
        self._opts = {
            "--temp-dir-base": temp_dir_base,
            "--data-dir": None,
            "--temp-dir-destroy": "on-success",
            "--temp-sweep-age-days": age_days,
        }

    def getoption(self, name, default=None):
        return self._opts.get(name, default)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ROOT_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _config(sweeper=None, node_batch_ids=None, **kw):
    cfg = Cfg(**kw)
    ctx = SessionContext(working_dir="/w", run_id=RUN_ID)
    if node_batch_ids is not None:
        ctx.plan = Plan(node_batch_ids=node_batch_ids)
    set_context(cfg, ctx)
    if sweeper is not None:
        register_temp_sweeper(cfg, sweeper)
    return cfg


# -----------------------------------------------------------------------------
# Registration seam
#


def test_register_temp_sweeper_stores_on_context(clean_env):
    from ducktest import get_temp_sweeper

    cfg = _config()
    assert get_temp_sweeper(cfg) is None  # none registered => no-op
    sweeper = FakeSweeper()
    register_temp_sweeper(cfg, sweeper)
    assert get_temp_sweeper(cfg) is sweeper


# -----------------------------------------------------------------------------
# The one session-end sweep — keep-on-failure keep-list, remote-only, sweeper-gated
#


def test_all_pass_sweeps_with_empty_keeplist(clean_env):
    # No failures recorded → keep nothing → everything under the session prefix is swept.
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids={"t/a.test": "batch-0"})
    sweep_session(cfg)
    assert sweeper.swept == [(_session_prefix(), [])]


def test_failed_test_keeps_its_batch(clean_env):
    sweeper = FakeSweeper()
    cfg = _config(
        sweeper,
        node_batch_ids={"t/a.test": "batch-2", "t/b.test": "batch-2", "t/c.test": "batch-5"},
    )
    record_failure(cfg, Rep("t/a.test", failed=True))  # a.test failed → keep its whole batch (batch-2)
    sweep_session(cfg)
    assert sweeper.swept == [(_session_prefix(), ["batch-2/**"])]


def test_multiple_failed_batches_each_kept(clean_env):
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids={"t/a.test": "batch-2", "t/c.test": "batch-5"})
    record_failure(cfg, Rep("t/a.test", failed=True))
    record_failure(cfg, Rep("t/c.test", failed=True))
    sweep_session(cfg)
    assert sweeper.swept == [(_session_prefix(), ["batch-2/**", "batch-5/**"])]


def test_record_failure_ignores_passes(clean_env):
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids={"t/a.test": "batch-1"})
    record_failure(cfg, Rep("t/a.test", failed=False))  # a pass is not recorded
    sweep_session(cfg)
    assert sweeper.swept == [(_session_prefix(), [])]  # nothing kept → all swept


def test_unmapped_failure_keeps_whole_session(clean_env):
    # A failed node not in the batch map (e.g. a driver .py) → keep the WHOLE session (coarsest safe).
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids={"t/a.test": "batch-2"})
    record_failure(cfg, Rep("driver/x.py::test_z", failed=True))
    sweep_session(cfg)
    assert sweeper.swept == [(_session_prefix(), ["**"])]


def test_no_batch_map_keeps_whole_session_on_failure(clean_env):
    # No plan/map at all + a failure → keep everything (never over-sweep a failed test's artifacts).
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids=None)
    record_failure(cfg, Rep("t/a.test", failed=True))
    sweep_session(cfg)
    assert sweeper.swept == [(_session_prefix(), ["**"])]


def test_local_root_is_noop(clean_env):
    # A local <root> => local is the binary's job, driver does nothing.
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids={"t/a.test": "batch-0"}, temp_dir_base="/tmp/base")
    record_failure(cfg, Rep("t/a.test", failed=True))
    sweep_session(cfg)
    assert sweeper.swept == []


def test_no_sweeper_is_noop(clean_env):
    cfg = _config(sweeper=None, node_batch_ids={"t/a.test": "batch-0"})  # remote, but nothing registered
    record_failure(cfg, Rep("t/a.test", failed=True))
    sweep_session(cfg)  # must not raise


def test_interruption_without_sessionfinish_does_no_sweep(clean_env):
    # A run that never reaches sessionfinish never calls sweep_session → the sweeper is untouched (the
    # age-sweep is the backstop). Modeled here by recording a failure but NOT invoking sweep_session.
    sweeper = FakeSweeper()
    cfg = _config(sweeper, node_batch_ids={"t/a.test": "batch-0"})
    record_failure(cfg, Rep("t/a.test", failed=True))
    assert sweeper.swept == []


# -----------------------------------------------------------------------------
# Age-sweep (backstop, best-effort) — lists under <root>, full-purge stale sessions
#


def _session_id(days_ago):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return f"{d.strftime('%Y%m%d')}_stale_mnem"


def test_age_sweep_purges_only_stale_prefixes(clean_env):
    old, fresh = _session_id(10), _session_id(1)
    listing = [
        (f"{REMOTE}/{old}", old),
        (f"{REMOTE}/{fresh}", fresh),
    ]
    sweeper = FakeSweepSweeper(listing)
    cfg = _config(sweeper, age_days=7)
    age_sweep(cfg)
    # only the >7d prefix, full-purged (empty keep-list); the fresh one is kept
    assert sweeper.swept == [(f"{REMOTE}/{old}", [])]


def test_age_sweep_tolerates_bad_prefix_and_sweep_error(clean_env):
    old = _session_id(30)
    listing = [
        (f"{REMOTE}/{old}", old),  # stale → purged
        (f"{REMOTE}/garbage", "garbage"),  # unparseable session-id → skipped, not fatal
        (f"{REMOTE}/{_session_id(40)}explode", _session_id(40)),  # stale but sweep raises → doesn't abort
    ]
    sweeper = FakeSweepSweeper(listing)
    cfg = _config(sweeper, age_days=7)
    age_sweep(cfg)  # must not raise
    assert sweeper.swept == [(f"{REMOTE}/{old}", [])]  # the good stale one still got swept


def test_age_sweep_noop_without_list_support(clean_env):
    # A sweeper without list_run_prefixes → the age-sweep is silently skipped (the session sweep still works).
    sweeper = FakeSweeper()
    cfg = _config(sweeper)
    age_sweep(cfg)
    assert sweeper.swept == []


def test_age_sweep_local_root_is_noop(clean_env):
    sweeper = FakeSweepSweeper([(f"{REMOTE}/{_session_id(99)}", _session_id(99))])
    cfg = _config(sweeper, temp_dir_base="/tmp/base")
    age_sweep(cfg)
    assert sweeper.swept == []


# -----------------------------------------------------------------------------
# End-to-end: the session-end sweep actually fires at sessionfinish under a real run
#


def test_session_sweep_wired_through_real_pytest(pytester, monkeypatch):
    """Prove the wiring (controller logreport → sessionfinish → sweep_session) under a real run: a
    conftest registers a sweeper that logs its sweep call; one test passes, one fails. Remoteness comes
    from a remote --temp-dir-base. The failing tests are plain `.py` (not batched SqlLogic items), so
    they aren't in the node→batch map → the sweep keeps the WHOLE session (the coarse under-sweep-safe
    fallback), proving the failure was collected on the controller and the sweep fired exactly once."""
    sweep_log = pytester.path / "sweep.log"
    monkeypatch.setenv("SWEEP_LOG", str(sweep_log))
    pytester.makeconftest(
        textwrap.dedent(
            """
            import json
            import os
            from ducktest import register_temp_sweeper

            class LogSweeper:
                def sweep(self, prefix, keep_patterns):
                    with open(os.environ["SWEEP_LOG"], "a") as f:
                        f.write(json.dumps([prefix, list(keep_patterns)]) + "\\n")

            def pytest_configure(config):
                register_temp_sweeper(config, LogSweeper())
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
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider", "--temp-dir-base", REMOTE)
    result.assert_outcomes(passed=1, failed=1)
    import json

    lines = [ln for ln in sweep_log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1  # exactly one session-end sweep (the age-sweep no-ops: LogSweeper can't list)
    prefix, keeps = json.loads(lines[0])
    assert prefix.startswith(REMOTE + "/") and prefix.endswith("/")  # <root>/<session-id>/
    assert keeps == ["**"]  # a failure with no batch map → keep the whole session (under-sweep safe)
