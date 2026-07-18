"""Collect-first controller: SCAN → PLAN → EXECUTE — the redesign's heart.

The shipped driver decided *what to provision* from the invocation ARGS, before workers collected
(under xdist the controller never collects — workers do, after they fork). Args can't resolve `-k`,
so it needed a from-args predictor (`_suite_reachable`, which re-implemented pytest's `-m` engine)
PLUS a reactive `pytest_runtest_setup` backstop to catch the `-k` case the predictor couldn't see.
Two mechanisms for one decision.

The xdist spike proved the cure and this adopts it: on the controller, `pytest_collection`
(tryfirst) calls `session.perform_collect()` ONCE — resolving the real selected set (`-k` included)
before any worker does real work (~1-2% overhead, mostly overlapped). We then know exactly what runs,
so provisioning derives from the TRUTH, not a prediction. The predictor and the backstop both delete.

Spike rules encoded here (do not rediscover):
  - hook is `pytest_collection`, tryfirst — NOT `pytest_sessionstart` (two crash traps there).
  - wrap the manual collect in `warnings.catch_warnings()` (else import-time warnings double-count).
  - it is not literally pre-fork (execnet gateways exist) but fires before worker import/collection.
"""

from __future__ import annotations

import warnings
from typing import Optional

import pytest

from .context import Plan, SessionContext, get_context


class Controller:
    """The controller-side collect-first orchestrator. Registered as a plugin ONLY on the controller
    (and on `-n0` serial runs, which are their own controller). Workers never run this — they collect
    normally and consume the plan/store the controller published."""

    def __init__(self, ctx: SessionContext):
        self.ctx = ctx

    # SCAN — drive the one authoritative collection, build the Plan --------------------------------

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> Optional[bool]:
        """Own collection on the controller: stamp suite auto-markers, collect once (so `-m`/`-k` are
        really resolved), reconcile against the binary, then provision what the selection needs.

        Returning True tells pytest collection is handled — but we DON'T short-circuit the normal
        flow; we let pytest proceed so items exist for `--repl`/reporting. We call `perform_collect`
        to KNOW the selection early; pytest's own subsequent collection is idempotent for our needs.
        """
        _stamp_suite_markers(self.ctx, session)  # markers must exist before -m/-k deselect

        with warnings.catch_warnings():  # spike: avoid double-counting import-time warnings
            warnings.simplefilter("ignore")
            items = session.perform_collect(genitems=True)

        plan = self._build_plan(session, items)
        self.ctx.plan = plan

        _reconcile_or_die(self.ctx, plan)  # verify mode: hard-error on false-green divergence
        self._provision_up_front(plan)  # fetch creds (prompt once here) + boot needed services

        return None  # let pytest's normal collection proceed; we only needed the early truth

    def _build_plan(self, session: pytest.Session, items: list) -> Plan:
        """From the REAL selected items, derive reachable suites + needed creds/services. No predictor:
        membership comes from the marker each item actually carries, which is exactly what `-m`/`-k`
        just selected on."""
        selected = frozenset(it.nodeid for it in items)
        reachable = set()
        for it in items:
            for name, suite in self.ctx.registry.suites.items():
                if it.get_closest_marker(suite.marker) is not None:
                    reachable.add(name)

        creds, svcs = set(), set()
        for name in reachable:
            suite = self.ctx.registry.suites[name]
            creds.update(c.key for c in getattr(suite, "credentials", ()))
            svcs.update(s.key for s in getattr(suite, "services", ()))

        fs_names = frozenset(getattr(it, "_test_name", it.nodeid) for it in items)
        return Plan(
            selected_nodeids=selected,
            reachable_suites=frozenset(reachable),
            needed_credentials=frozenset(creds),
            needed_services=frozenset(svcs),
            fs_names=fs_names,
            binary_names=self._binary_names_if_any(),
        )

    def _binary_names_if_any(self) -> frozenset[str]:
        """List the binary's registered set for the reconcile — only if a `.test` lane is in play and
        a binary was resolved (a pure-Python suite run needs no binary, and mustn't fail for lack of
        one)."""
        if not self.ctx.binary:
            return frozenset()
        from .collect import list_binary_tests

        try:
            return list_binary_tests(self.ctx.binary, working_dir=self.ctx.working_dir)
        except RuntimeError:
            # too-old binary: surfaced by the reconcile path as a config error, not a silent skip
            return frozenset()

    # EXECUTE — provision up front, from the plan, on the controller -------------------------------

    def _provision_up_front(self, plan: Plan) -> None:
        """Fetch reachable credentials (the one place an interactive prompt lands — pre-fork, on the
        controller) and boot the services the selection needs, publishing both to the store + env.

        This is the SINGLE provisioning entry the spec calls for: because the plan already knows the
        full selection, a bare driverless `.test` that needs a service is handled here just like a
        `.py`-driven one — there is no `eager` vs `on_demand` disposition fork, no pre-fork-vs-
        fixture-pull duality. Workers consume; they never provision.
        """
        if not (plan.needed_credentials or plan.needed_services):
            return  # vanilla run: never even start the store (pinned invariant)

        store = _ensure_store(self.ctx)
        for name in sorted(plan.reachable_suites):
            suite = self.ctx.registry.suites[name]
            for cred in getattr(suite, "credentials", ()):
                if cred.key in plan.needed_credentials:
                    _fetch_credential(store, cred)  # fetch → validate → store.put → adopt(env)
            for svc in getattr(suite, "services", ()):
                if svc.key in plan.needed_services:
                    from .provision import provision_service

                    provision_service(self.ctx, svc)  # attach-or-single-flight-boot; publish env


# --- module helpers (kept out of the class so workers can call the marker stamp too) -----------


def _stamp_suite_markers(ctx: SessionContext, session: pytest.Session) -> None:
    """Turn path-membership into marker-membership so `-m cloud` selects markerless `.test` bodies.

    In the shipped code this had to run in a collection hookwrapper *pre-yield* so marks existed
    before pytest's builtin `-m`/`-k` deselection read them — "the whole trick." Here, because the
    controller DRIVES `perform_collect`, we stamp before calling it: the ordering is explicit and
    local, not a pluggy-ordering side effect. (Workers, which collect independently, call this from
    their own collection hook — see plugin.py.)
    """
    # Implementation note: stamping walks the session's initial paths and applies each suite's marker
    # to items under the suite's tree. The concrete walk lives with the collection hooks in plugin.py;
    # this function is the single named entry both controller and worker paths call.
    from .plugin import apply_suite_markers

    apply_suite_markers(ctx, session)


def _reconcile_or_die(ctx: SessionContext, plan: Plan) -> None:
    """verify mode: refuse a false-green run where the binary knows tests the FS didn't collect."""
    if not plan.binary_names:
        return  # no `.test` lane / no binary — nothing to reconcile
    from .collect import reconcile

    mode = ctx.options.get("collect_source", "verify")
    reconcile(plan.fs_names, plan.binary_names, mode=mode).check()


def _ensure_store(ctx: SessionContext):
    """Start the shared store lazily — only when a reachable suite actually needs it (so a vanilla
    run never starts it, a pinned invariant). Idempotent."""
    if ctx.store is None:
        from . import store as store_mod

        ctx.store = store_mod.start_server()
    return ctx.store


def _fetch_credential(store, cred) -> None:
    """Up-front credential fetch on the controller: fetch → validate → publish (poison on invalid) →
    adopt into os.environ so it's inherited by forked workers. The single-flight + poison-pill live
    in the store; this is the thin driver of the descriptor's own callables."""
    import os

    def factory():
        block = cred.fetch()
        if cred.validate and not cred.validate(block):
            raise ValueError(f"credential {cred.key!r} failed validation")
        return block

    block = store.copy_or_provision(cred.key, factory)
    if getattr(cred, "adopt", None) == "env":
        os.environ.update({k: str(v) for k, v in block.items()})
