"""Provisioning: the per-test Provisioner protocol + the single service provisioning entry.

Two things live here:

1. **`provision_service(ctx, svc)`** — the ONE routing point for suite-level services (lifted out of
   the shipped `plugin.py`). Attach-or-boot: if the service key is in `--existing-service`, ATTACH
   (build the block, probe `alive`, populate idempotently) and never enter the store or tear it down;
   else MANAGED (single-flight boot via the store). Either way `to_env(block)` is merged into the
   caller's environment. A test cannot tell which stance ran — identical block shape (the block/derive
   builder contract in the resource modules guarantees that). Because the collect-first controller
   calls this UP FRONT for every service the plan needs, there is no eager/on_demand disposition fork:
   provisioning has one entry and one timing.

2. **`Provisioner` base + registry** — the generic per-`@requires` access-policy loop (rw -> isolated
   namespace instantiated + tracked for teardown; ro -> shared target instantiated once per session).
   A backend subclasses and fills the hooks. **Redesign change vs the shipped base:** `provision()`
   returns the *framework* `Bindings` (context.Bindings: token/isolated/env/summary + an opaque
   `backend` payload) — the backend's catalog/default_schema/tables no longer masquerade as "generic"
   fields the base never reads; they live in `.backend`. The registry is the one typed
   `context.Registry` (scope REQUIRED, resolved nearest-ancestor — no `path=None` back-compat branch).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .context import Bindings, get_context


# --- registry (thin adapters over the one typed Registry) -------------------------------------


def register_provisioner(config, provisioner, scope) -> None:
    """Register a backend provisioner scoped to the registering conftest's directory (call from a
    conftest `pytest_configure`; pass `scope=os.path.dirname(__file__)`). Resolution is by TEST
    LOCATION (nearest-ancestor) so a mixed multi-backend selection resolves each test to ITS backend.

    `scope` is required — the shipped `scope=None` global-fallback / `path=None` most-recent-wins
    branches are gone (a legacy accommodation; the location is always knowable)."""
    if scope is None:
        raise ValueError("register_provisioner: scope is required (the registering conftest's dir)")
    get_context(config).registry.register_provisioner(os.path.abspath(str(scope)), provisioner)


def get_provisioner(config, path) -> Optional[Any]:
    """The provisioner whose scope is the nearest ancestor of `path`, else None. `path` required."""
    return get_context(config).registry.provisioner_for(str(path))


# --- the single service provisioning entry ----------------------------------------------------


def provision_service(config, svc) -> dict:
    """Attach-or-boot `svc`, publish its env, return the block. The one entry (spec §3.6).

    Config-first (the descriptors' `start`/`fetch`/`attach` callables all take a pytest config, and
    the store + `--existing-service` resolution hang off the session context on the config). The
    concrete routing (attach vs single-flight managed boot, `to_env` adoption) lives with the store
    lifecycle in :mod:`ducktest.plugin`; this is the stable public entry that delegates to it.

    Idempotent + single-flight: two workers racing the same managed service boot it once (the store
    serializes); an attach just re-probes. Never booted twice, never a leaked half-boot in the store.
    """
    from .plugin import provision_service as _impl

    return _impl(config, svc)


# --- the per-@requires Provisioner base -------------------------------------------------------


@dataclass
class State:
    """The rich WORKING object a backend's hooks populate during `provision()`. Its data becomes the
    opaque `Bindings.backend` payload at the return boundary — so the FRAMEWORK never sees
    catalog/default_schema/tables (it reads only token/isolated/env/summary), but the backend keeps
    the ergonomic rich structure its `instantiate`/`rw_target`/`make_init_sql` need. Public so a
    backend can construct it in `new_state` (returning it, or a subclass with extra fields)."""

    token: str
    catalog: Optional[str] = None
    default_schema: Optional[str] = None
    tables: list = field(default_factory=list)
    isolated: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    plan: list = field(default_factory=list)


class Provisioner:
    """Base Provisioner: the generic access-policy spec-loop + RO once-guard + teardown. A backend
    overrides the hooks (`execute`, `rw_target`, `ro_target`, `instantiate`, `make_init_sql` required;
    the rest have defaults). `provision()` returns the framework `Bindings`; backend-shaped data is in
    `Bindings.backend` (a `State`, or a backend subclass of it via `new_state`)."""

    def __init__(self):
        # RO targets instantiated once this INVOCATION. Store-backed single-flight across workers
        # when a store is running (see `_ro_already_shared`); this bare set is the fallback for when
        # it isn't (e.g. a provisioner-only suite declaring no service/credential never starts one) --
        # same as the shipped per-worker behavior, and also the always-used path in `dry_run` (a
        # preview must never claim a real cross-worker slot).
        self._shared_ro = set()

    def provision(self, specs, token, *, dry_run=False, params=None, config=None) -> Bindings:
        """`config` (optional) is the pytest config -- passed by the framework's call sites so the
        RO once-guard can single-flight through the store; omit it (as direct/unit-test callers do)
        to fall back to the bare per-process guard. See `_ro_already_shared`."""
        self.before_provision(specs, token, dry_run)
        state = self.new_state(token, params=params)
        for spec in specs:
            if spec.access == "rw":
                target = self.rw_target(spec, token, state, dry_run)
                self.instantiate(spec, target, dry_run, state)
            else:
                target = self.ro_target(spec, state)
                if self._ro_already_shared(spec, target, state, dry_run, config):
                    state.plan.append(f"[ro] {target} already provisioned this session")
        self.finalize_state(state)
        state.env = self.env_for(state)
        if dry_run:
            print("provision plan (NO DDL executed):")
            for line in state.plan:
                print(f"  {line}")
            self.dry_run_summary(state)
        return self._freeze(state)

    def _freeze(self, state: State) -> Bindings:
        """The Bindings SPLIT realized at one boundary: framework fields promoted, rich state opaque."""
        return Bindings(
            token=state.token,
            isolated=tuple(state.isolated),
            env=dict(state.env),
            summary="\n".join(state.plan),
            backend=state,
        )

    def teardown(self, bindings: Optional[Bindings] = None) -> None:
        """Drop the isolated `rw` namespaces this provision created — catalog metadata only.

        Physical-storage reclaim moved OUT of teardown (SPEC §11.4): a remote root is swept by the
        driver's path-addressed prefix delete of `${TEMP_DIR}/${token}/`, which is order-independent
        — so the old `reclaim_physical`-before-`drop_sql` LIFO ordering (and the `reclaim_physical`
        hook itself) is gone. Teardown now only drops namespaces. (`bindings.isolated` is the only
        framework field the base reads.)"""
        isolated = list(bindings.isolated) if bindings else []
        for ns in isolated:
            self.execute(self.drop_sql(ns))

    def _ro_already_shared(self, spec, target, state, dry_run, config) -> bool:
        """Instantiate `target` at most once THIS INVOCATION; return whether it was already done
        (by us or another worker) so the caller appends the "already provisioned" plan line instead.

        Store-backed single-flight across workers when a store is running -- the same coordination
        primitive `Service`/`Credential` already use (`store.copy_or_provision`), promoted here per
        RESOURCE-PLANNING.md §4's validation: RO instantiation is typically an idempotent
        `CREATE ... IF NOT EXISTS`, safe to dedupe the same way. A failed instantiate poisons the key
        for the rest of the session (no retry storm) -- the same contract every other store-coordinated
        resource already has, not a new one invented here.

        Falls back to the bare per-process `_shared_ro` set when no store is running (a
        provisioner-only suite that declares no service/credential never starts one) -- identical to
        the shipped per-worker behavior. `dry_run` ALWAYS uses that same bare-set path: a preview must
        never claim a real cross-worker slot.
        """
        if dry_run or config is None:
            if target in self._shared_ro:
                return True
            self.instantiate(spec, target, dry_run, state)
            if not dry_run:
                self._shared_ro.add(target)
            return False

        from .plugin import get_store

        handle = get_store(config)
        if handle is None:
            if target in self._shared_ro:
                return True
            self.instantiate(spec, target, dry_run, state)
            self._shared_ro.add(target)
            return False

        from . import store as _store

        ran = False

        def _factory():
            nonlocal ran
            self.instantiate(spec, target, dry_run, state)
            ran = True
            return True

        _store.copy_or_provision(handle, self._ro_store_key(target), _factory)
        return not ran

    def _ro_store_key(self, target) -> str:
        """Store key for a RO target -- namespaced by this Provisioner's concrete class so two
        different backends sharing one store never collide even if a target string coincides."""
        cls = type(self)
        return f"ro::{cls.__module__}.{cls.__qualname__}::{target}"

    def ensure_isolated(self, namespace, state, dry_run, *, create_sql=None):
        if namespace in state.isolated:
            return
        state.isolated.append(namespace)
        sql = create_sql if create_sql is not None else f"CREATE SCHEMA IF NOT EXISTS {namespace}"
        state.plan.append(f"{sql};")
        if not dry_run:
            self.execute(sql)

    # -- hooks a backend implements (state is the rich State / a subclass) --

    def before_provision(self, specs, token, dry_run):
        """Optional up-front validation before any spec is provisioned. Default: no-op."""

    def new_state(self, token, *, params=None) -> State:
        """Construct the working state. Default bare `State(token)`; override to preset
        catalog/default_schema or return a `State` subclass with extra backend fields."""
        return State(token=token)

    def execute(self, sql):
        raise NotImplementedError

    def rw_target(self, spec, token, state, dry_run):
        raise NotImplementedError

    def ro_target(self, spec, state):
        raise NotImplementedError

    def instantiate(self, spec, target, dry_run, state):
        raise NotImplementedError

    def finalize_state(self, state):
        """Called once after the spec loop, before `env_for`. Default: no-op."""

    def env_for(self, state) -> dict:
        return {}

    def drop_sql(self, namespace) -> str:
        return f"DROP SCHEMA IF EXISTS {namespace} CASCADE"

    def dry_run_summary(self, state):
        """Optional extra dry-run print after the plan. Default: no-op."""

    def make_init_sql(self, bindings, *, redact: bool = False) -> str:
        """Concrete duckdb init SQL for the `--repl` launch. `redact=True` masks secrets for display.
        Receives the framework `Bindings`; read backend data via `bindings.backend`. Required."""
        raise NotImplementedError
