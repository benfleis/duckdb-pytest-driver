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
class _State:
    """The rich WORKING object a backend's hooks populate during `provision()`. Its data becomes the
    opaque `Bindings.backend` payload at the return boundary — so the FRAMEWORK never sees
    catalog/default_schema/tables (it reads only token/isolated/env/summary), but the backend keeps
    the ergonomic rich structure its `instantiate`/`rw_target`/`make_init_sql` need."""

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
    `Bindings.backend` (a `_State`, or a backend subclass of it via `new_state`)."""

    def __init__(self):
        self._shared_ro = set()  # RO targets instantiated once per session (per worker under xdist)

    def provision(self, specs, token, *, dry_run=False, params=None) -> Bindings:
        self.before_provision(specs, token, dry_run)
        state = self.new_state(token, params=params)
        for spec in specs:
            if spec.access == "rw":
                target = self.rw_target(spec, token, state, dry_run)
                self.instantiate(spec, target, dry_run, state)
            else:
                target = self.ro_target(spec, state)
                if target in self._shared_ro:
                    state.plan.append(f"[ro] {target} already provisioned this session")
                else:
                    self.instantiate(spec, target, dry_run, state)
                    if not dry_run:
                        self._shared_ro.add(target)
        self.finalize_state(state)
        state.env = self.env_for(state)
        if dry_run:
            print("provision plan (NO DDL executed):")
            for line in state.plan:
                print(f"  {line}")
            self.dry_run_summary(state)
        return self._freeze(state)

    def _freeze(self, state: _State) -> Bindings:
        """The Bindings SPLIT realized at one boundary: framework fields promoted, rich state opaque."""
        return Bindings(
            token=state.token,
            isolated=tuple(state.isolated),
            env=dict(state.env),
            summary="\n".join(state.plan),
            backend=state,
        )

    def teardown(self, bindings: Optional[Bindings] = None) -> None:
        """Drop each namespace tracked in `bindings.isolated` (the ONLY teardown input the base reads).

        Redesign note: a backend that also stages physical storage (parquet/delta files under a `rw`
        prefix) overrides `reclaim_physical(bindings)` — the shipped base dropped catalog metadata
        only, so every `rw` provision leaked files. The base calls it after the namespace drop."""
        isolated = list(bindings.isolated) if bindings else []
        for ns in isolated:
            self.execute(self.drop_sql(ns))
        if bindings is not None:
            self.reclaim_physical(bindings)

    def ensure_isolated(self, namespace, state, dry_run, *, create_sql=None):
        if namespace in state.isolated:
            return
        state.isolated.append(namespace)
        sql = create_sql if create_sql is not None else f"CREATE SCHEMA IF NOT EXISTS {namespace}"
        state.plan.append(f"{sql};")
        if not dry_run:
            self.execute(sql)

    # -- hooks a backend implements (state is the rich _State / a subclass) --

    def before_provision(self, specs, token, dry_run):
        """Optional up-front validation before any spec is provisioned. Default: no-op."""

    def new_state(self, token, *, params=None) -> _State:
        """Construct the working state. Default bare `_State(token)`; override to preset
        catalog/default_schema or return a `_State` subclass with extra backend fields."""
        return _State(token=token)

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

    def reclaim_physical(self, bindings) -> None:
        """Reclaim physical storage a `rw` provision staged (files/objects), not just catalog metadata.
        Default: no-op (a catalog-only backend has nothing to reclaim). Object-store backends override
        — the seam that fixes the shipped base's `rw` storage leak."""

    def dry_run_summary(self, state):
        """Optional extra dry-run print after the plan. Default: no-op."""

    def make_init_sql(self, bindings, *, redact: bool = False) -> str:
        """Concrete duckdb init SQL for the `--repl` launch. `redact=True` masks secrets for display.
        Receives the framework `Bindings`; read backend data via `bindings.backend`. Required."""
        raise NotImplementedError
