"""Session-scoped context + typed registry — the redesign's answer to `config._duckdb_*` sprawl.

The shipped driver stashed ~everything on the pytest `config` object as ad-hoc string attributes
(`config._duckdb_suites`, `config._driver_provisioners`, `config._driver_instantiators`,
`config._duckdb_existing_services`, `config.sqllogic_working_dir`, `config._sqllogic_run_id`, …) —
four registries + a scatter of cached invariants, each looked up by a bare string in a dozen places.

Here there is ONE typed object, built once at `pytest_configure`, stored in pytest's typed `Stash`
(not a string attribute), and read by every downstream component. It carries the session invariants
(resolved binary, working dir, run-id, options) and the single `Registry` (suites + provisioners +
instantiators + broadcasts, one idiom instead of four). The collect-first controller fills in `plan`
after it scans; nothing recomputes `find_binary` per item (which is what made the old fail-fast bug
fire lazily, once per collection root, instead of once at configure).

Dependency-light on purpose (stdlib only): this is the foundation the spine and the ported modules
both import, so it must not pull in the store, the descriptors, or a backend.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

# --- the typed stash key: how everyone gets the context off `config` --------------------------

# A single typed key. `config.stash[CONTEXT_KEY]` replaces every `config._duckdb_*` string attr.
CONTEXT_KEY: pytest.StashKey["SessionContext"] = pytest.StashKey()


def get_context(config: pytest.Config) -> "SessionContext":
    """The session context for this run. Present after `pytest_configure` (raises if asked earlier —
    a loud failure beats a silent `getattr(config, "_duckdb_x", None)` returning None)."""
    return config.stash[CONTEXT_KEY]


def set_context(config: pytest.Config, ctx: "SessionContext") -> None:
    config.stash[CONTEXT_KEY] = ctx


# --- the single registry (was four `config._ATTR` strings) ------------------------------------


@dataclass
class Registry:
    """The one place backends register into, resolved by test-location scope where that matters.

    Suites are global (a run has one suite set). Provisioners and instantiators are scoped by the
    directory subtree they were registered under, resolved nearest-ancestor — because a mixed
    selection (two extensions in one run) must not let a global last-wins clobber the other. Same
    idiom for both, instead of the shipped code's two near-identical hand-rolled maps.
    """

    suites: dict[str, Any] = field(default_factory=dict)  # name -> Suite descriptor
    _provisioners: list[tuple[str, Any]] = field(default_factory=list)  # (scope_path, Provisioner)
    _instantiators: list[tuple[str, Any]] = field(default_factory=list)  # (scope_path, Instantiator)
    broadcasts: dict[str, Any] = field(default_factory=dict)  # controller-computed, worker-read

    def register_suite(self, suite: Any) -> None:
        if suite.name in self.suites:
            raise ValueError(f"duplicate suite name {suite.name!r}")
        self.suites[suite.name] = suite

    # Scoped registries: register with the dir the descriptor lives under; resolve nearest-ancestor.
    def register_provisioner(self, scope_path: str, prov: Any) -> None:
        self._provisioners.append((scope_path, prov))

    def register_instantiator(self, scope_path: str, inst: Any) -> None:
        self._instantiators.append((scope_path, inst))

    def provisioner_for(self, path: str) -> Optional[Any]:
        return _nearest(self._provisioners, path)

    def instantiator_for(self, path: str) -> Optional[Any]:
        return _nearest(self._instantiators, path)


def _nearest(scoped: list[tuple[str, Any]], path: str) -> Optional[Any]:
    """Longest registered scope that is an ancestor of (or equals) `path`. None if unmatched.

    No `path=None -> most-recently-registered` back-compat branch (the shipped code's legacy
    accommodation): the location is always required, so resolution is unambiguous.
    """
    import os.path

    best: Optional[tuple[int, Any]] = None
    ap = os.path.abspath(path)
    for scope, obj in scoped:
        asc = os.path.abspath(scope)
        if ap == asc or ap.startswith(asc + os.sep):
            depth = asc.count(os.sep)
            if best is None or depth > best[0]:
                best = (depth, obj)
    return best[1] if best else None


# --- the Plan: what collect-first produces, replacing the from-args predictor -----------------


@dataclass
class Plan:
    """The output of the controller's SCAN phase — the *real* selected set, not a prediction.

    Because it is built from `session.perform_collect()` on the controller (collect-first), `-k` is
    already resolved. Provisioning (which credentials to fetch, which services to boot) derives from
    `needed_credentials`/`needed_services` here — so there is no predictive `_suite_reachable` and no
    reactive `pytest_runtest_setup` backstop; the two collapse into this one object.
    """

    selected_nodeids: frozenset[str] = frozenset()
    reachable_suites: frozenset[str] = frozenset()  # suite names a selected item belongs to
    needed_credentials: frozenset[str] = frozenset()  # credential keys the selection requires
    needed_services: frozenset[str] = frozenset()  # service keys the selection requires
    # collection provenance, for the verify/authoritative reconcile (kills false-green):
    fs_names: frozenset[str] = frozenset()
    binary_names: frozenset[str] = frozenset()

    @property
    def collection_divergence(self) -> frozenset[str]:
        """Names the binary knows that the FS walk missed (or vice-versa) — the false-green set.
        `verify` mode hard-errors if this is non-empty; `authoritative` mode collects the union."""
        return self.fs_names ^ self.binary_names

    def as_dict(self) -> dict:
        """JSON-able view of the scan/plan for `--emit-plan` (frozensets -> sorted lists). This is the
        plan-as-artifact hand-off (SPEC §10.5): what collect-first resolved, for inspection now and
        for an alternate executor to consume later."""
        return {
            "selected_nodeids": sorted(self.selected_nodeids),
            "reachable_suites": sorted(self.reachable_suites),
            "needed_credentials": sorted(self.needed_credentials),
            "needed_services": sorted(self.needed_services),
            "collection": {
                "fs_names": sorted(self.fs_names),
                "binary_names": sorted(self.binary_names),
                "divergence": sorted(self.collection_divergence),
            },
        }


# --- Bindings: framework-owned fields separated from backend payload ---------------------------


@dataclass
class Bindings:
    """What a per-test `provision()` returns. The FRAMEWORK owns only four fields and reads only
    these; anything backend-shaped lives under `backend`, explicitly — not smuggled through
    "generic" `catalog`/`tables` fields the base never touches (the shipped `Bindings` did that).

      token    - the per-test isolation id (`<date>_<mnemonic>_<nodeid-sha1[:6]>`)
      isolated - namespaces this test created and must drop on teardown (the ONLY teardown input)
      env      - vars merged into the paired subprocess's environment (the ONLY consume path)
      summary  - human dry-run text (was `.plan`; renamed to free `Plan` for the session object)
      backend  - opaque, backend-defined payload (catalog/default_schema/tables/URIs/…)
    """

    token: str
    isolated: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    backend: Any = None

    def with_backend(self, payload: Any) -> "Bindings":
        return dataclasses.replace(self, backend=payload)


# --- the session context ----------------------------------------------------------------------


@dataclass
class SessionContext:
    """Built once at `pytest_configure`; the single carrier of session invariants + the registry.

    `binary`/`duckdb_cli`/`working_dir` are resolved ONCE here (fail-fast on `--build auto`
    ambiguity happens at construction, session-level, not lazily per collection root). `plan` is
    None until the collect-first controller fills it in the SCAN phase. `store` is an opaque handle
    (typed Any to keep this module import-cycle-free); it's None until a suite that declares a
    credential/service starts it.
    """

    working_dir: str
    run_id: str
    options: dict[str, Any] = field(default_factory=dict)
    registry: Registry = field(default_factory=Registry)
    binary: Optional[str] = None  # resolved duckdb `unittest` binary (None until a lane needs it)
    duckdb_cli: Optional[str] = None  # resolved duckdb CLI (for provisioner init-SQL / --repl)
    store: Any = None  # store handle; started only if a reachable suite declares a resource
    plan: Optional[Plan] = None  # set by the controller's SCAN phase
    temp_reaper: Any = None  # REMOTE-storage reaper (purge/list) a backend registers; None => no-op

    # convenience passthroughs so callers read `ctx.suite(...)` not `ctx.registry.suites[...]`
    def suite(self, name: str) -> Any:
        return self.registry.suites.get(name)

    @property
    def existing_services(self) -> dict[str, Any]:
        """Parsed `--existing-service` map (key -> overrides|None); attach targets. Empty by default."""
        return self.options.get("existing_services", {})


# Marker/option resolution helpers live with the plugin; this module stays pure data + lookup.
BroadcastFactory = Callable[[pytest.Config], Any]
