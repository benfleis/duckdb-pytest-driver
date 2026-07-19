"""Collect-first controller: SCAN → PLAN → EXECUTE — the redesign's heart.

The shipped driver decided *what to provision* from the invocation ARGS, before workers collected
(under xdist the controller never collects — workers do, after they fork). Args can't resolve `-k`,
so it needed a from-args predictor (`_suite_reachable`, which re-implemented pytest's `-m` engine)
PLUS a reactive `pytest_runtest_setup` backstop to catch the `-k` case the predictor couldn't see.
Two mechanisms for one decision.

The xdist spike proved the cure and this adopts it: on the controller, `pytest_collection`
(tryfirst) calls `session.perform_collect()` ONCE — resolving the real selected set (`-k` included)
before any worker does real work. We then know exactly what runs, so provisioning derives from the
TRUTH, not a prediction. The predictor and the backstop both delete.

The Controller is registered as a plugin ONLY on the controller (and on `-n0` serial runs, which are
their own controller); workers never run it (they collect normally and consume the plan/store the
controller published). Every hook additionally self-gates on `workerinput` so a stray registration on
a worker is inert. The heavy lifting (store lifecycle, provisioning, reconcile, marker stamping) lives
in :mod:`ducktest.plugin`; this class owns the ordering.

Spike rules encoded here (do not rediscover):
  - hook is `pytest_collection`, tryfirst — NOT `pytest_sessionstart`.
  - wrap the manual collect in `warnings.catch_warnings()` (else import-time warnings double-count).
  - on a SERIAL run, RETURN TRUE so pytest doesn't collect a second time (double deselect counts);
    under xdist, RETURN NONE so xdist's own `pytest_collection` distributes to the workers.
"""

from __future__ import annotations

import warnings
from typing import Optional

import pytest

from .context import Plan, get_context


class Controller:
    """The controller-side collect-first orchestrator (see the module docstring)."""

    def __init__(self) -> None:
        self._config: Optional[pytest.Config] = None

    # -- store lifecycle + out-of-session service commands (before collection) ---------------------

    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config: pytest.Config) -> None:
        """trylast so this fires AFTER the consumer's `test/conftest.py` has registered its suites.

        Two controller-only, pre-collection jobs: (1) out-of-session `--provision-service` /
        `--teardown-service` (do the op + `pytest.exit`, needs no binary); (2) start the shared store
        + publish its address to the env PRE-FORK, so workers inherit it at spawn (`pytest_sessionstart`).
        """
        self._config = config  # held for pytest_runtest_logreport (which pytest passes no config)
        if getattr(config, "workerinput", None) is not None:
            return  # a worker connects to the controller's store; it starts none of its own
        from . import plugin

        if (
            config.getoption("--provision-service", default=None) is not None
            or config.getoption("--teardown-service", default=None) is not None
        ):
            plugin.run_service_command(config)  # does the op + pytest.exit(); never returns
            return
        with plugin._narrate_driver_log(config):
            plugin._ensure_store_started(config)

    # -- SCAN → PLAN → EXECUTE ---------------------------------------------------------------------

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> Optional[bool]:
        """Own collection on the controller: collect once (so `-m`/`-k` are really resolved), build the
        Plan, reconcile against the binary, then provision what the selection needs — all up front."""
        config = session.config
        if getattr(config, "workerinput", None) is not None:
            return None  # workers collect normally; the Controller shouldn't be here, but be inert

        from . import plugin

        ctx = get_context(config)
        with warnings.catch_warnings():  # spike: avoid double-counting import-time warnings
            warnings.simplefilter("ignore")
            items = session.perform_collect(genitems=True)

        plan = self._build_plan(config, items)
        ctx.plan = plan

        plugin.emit_plan(config, plan)  # --emit-plan: dump the scan/plan JSON BEFORE reconcile can abort
        plugin.reconcile_or_die(config, plan)  # verify mode: hard-error on false-green divergence
        plugin.provision_reachable(config, plan.reachable_suites)  # fetch creds + boot services, up front

        # Serial (`-n0`): WE collected — short-circuit so pytest's default `pytest_collection` doesn't
        # collect (and deselect) a second time. Under xdist: return None so xdist's `pytest_collection`
        # runs and distributes work to the workers (which collect independently).
        if not getattr(config.option, "numprocesses", 0):
            return True
        return None

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report) -> None:
        """Controller-side failed-node collection for the session-end remote keep-list (SPEC §11.5). The
        Controller is registered only on the controller, and xdist forwards worker reports here, so this
        sees every test's outcome without a worker-side stash. pytest passes no config → use `self._config`."""
        if self._config is None:
            return
        from .reaper import record_failure

        record_failure(self._config, report)

    def _build_plan(self, config: pytest.Config, items: list) -> Plan:
        """From the REAL selected items, derive reachable suites + needed creds/services + the reconcile
        provenance + the node-id→batch-id map. Membership comes from what `-m`/`-k` actually selected."""
        from . import plugin
        from .sqllogic import item_batch_id

        reachable = plugin._reachable_suites(config, items)
        suites = {t.name: t for t in plugin.get_suites(config)}
        creds, svcs = set(), set()
        for name in reachable:
            suite = suites[name]
            creds.update(c.key for c in suite.credentials)
            svcs.update(s.key for s in suite.services)

        fs_names = frozenset(getattr(it, "_test_name", it.nodeid) for it in items)
        # node-id → <batch-id> for the SqlLogic items (those carrying `_test_name`); the session-end
        # sweep maps a failed test to the batch dir to keep (SPEC §11.6). Batch numbering is
        # deterministic (collection order), so this matches the <batch-id> the worker composes.
        node_batch_ids = {
            it.nodeid: item_batch_id(it) for it in items if getattr(it, "_test_name", None) is not None
        }
        return Plan(
            selected_nodeids=frozenset(it.nodeid for it in items),
            reachable_suites=frozenset(reachable),
            needed_credentials=frozenset(creds),
            needed_services=frozenset(svcs),
            node_batch_ids=node_batch_ids,
            fs_names=fs_names,
            binary_names=plugin.binary_names_if_any(config),
        )
