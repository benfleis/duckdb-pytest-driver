"""The "decorate" step: one canonical per-item key, computed once between scan and plan.

`docs/RESOURCE-PLANNING.md` §2-3: every downstream decision (batching, resource sharing, token
generation, provisioning order) should be a projection or policy function over ONE key per test
item, instead of each mechanism inventing its own.

Phase 1 (§5) landed the data structure with its two invocation-constant fields — the same two
values `collect.py`'s `_batch_key` already computed ad hoc, just given a name/home. Phase 4 extends
`Key` with the three per-test-varying fields (`backend`/`access`/`cell`) and states the two sharing
guarantees the doc's §3 planner functions describe (`coordination_key`/`rw_identity` below) as
named projections over the key — **no generator changes**: `_provision_token` (plugin.py) and a
consumer's `cell_schema_name`-equivalent already uphold these by construction; this module states
what they uphold, so a caller can reason about sharing scope generically instead of re-deriving it
per backend. Populating `backend`/`access`/`cell` from a real item is left to whoever resolves it
(a `.py` driver's own `@requires`/`@requires_matrix`, not generically knowable from a bare item) —
`decorate()` accepts them as optional overrides so nothing here needs to change shape again when
that lands (deferred: matrix fan-out, `docs/RESOURCE-PLANNING.md` §5 phase 9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Key:
    """The canonical per-item key (growing; see module docstring).

    build       : which binary a test runs through. Invocation-constant today (one binary per run).
    run_setting : which whole-run config sweep (e.g. httpfs's `{curl, httplib, ...}`) — no driver
                  mechanism exists for this axis yet, so it's populated from the item's working dir,
                  the second value `_batch_key` already keyed batching affinity on. A real
                  `run_setting` axis, if ever built, replaces what feeds this field; it doesn't add
                  a new one.
    backend     : which account/service this test talks to (e.g. `azurite-az`, `uc-databricks`).
                  None where unresolved (no generic way to derive it from a bare item today).
    access      : `"rw"` (private, isolated) | `"ro"` (shared) | None where unresolved.
    cell        : the matrix cell id, if any (`@requires_matrix`'s per-cell identity); None for a
                  non-matrix test.
    """

    build: Optional[str]
    run_setting: Optional[str] = None
    backend: Optional[str] = None
    access: Optional[str] = None
    cell: Optional[str] = None


def decorate(item, *, backend=None, access=None, cell=None) -> Optional[Key]:
    """Compute `item`'s canonical `Key`, or None if `item` isn't a SqlLogic item (no
    `_binary`/`_working_dir` — the two attributes `SqlLogicItem`/`SqlLogicFile` stamp on collection).

    `backend`/`access`/`cell` are optional overrides for a caller that already knows them (e.g. a
    resolved `@requires` spec) — `decorate()` itself does no `@requires`/matrix introspection, to
    stay dependency-light; they default to None (today's behavior, unchanged).
    """
    build = getattr(item, "_binary", None)
    run_setting = getattr(item, "_working_dir", None)
    if build is None or run_setting is None:
        return None
    return Key(build=build, run_setting=run_setting, backend=backend, access=access, cell=cell)


# --- named guarantees, as pure projections of the key ------------------------------------------


def coordination_key(key: Optional[Key]) -> Optional[tuple]:
    """The RO-shared-identity guarantee (RESOURCE-PLANNING.md §3's `coordination_key`): two `ro`
    jobs coordinate around the SAME store slot iff their `(backend, access)` match — None means
    "nothing to single-flight" (an `rw` job is always private; an unresolved `backend` can't be
    identified at all, so it never accidentally collides with anything).

    This is a STATEMENT of the guarantee, not a new mechanism: `Provisioner._ro_store_key`
    (provision.py, phase 3) already computes an equivalent per-Provisioner-class store key by
    construction; this lets a caller reason about RO sharing scope generically, over the canonical
    key, independent of any one `Provisioner`'s own key derivation.
    """
    if key is None or key.access != "ro" or key.backend is None:
        return None
    return (key.backend, key.access)


def rw_identity(key: Optional[Key], nodeid: str) -> Optional[tuple]:
    """The RW-token-uniqueness guarantee: an `rw` job's provisioned namespace is unique per
    `(build, backend, nodeid)` — never shared with any other job's, regardless of `cell`. None for
    a non-`rw` key (an `rw` token is meaningless for `ro`/unresolved access).

    States, as a pure projection of the canonical key, the guarantee `_provision_token` (plugin.py)
    and a consumer's `cell_schema_name`-equivalent already uphold (a per-nodeid hash suffix); it does
    NOT replace either generator.
    """
    if key is None or key.access != "rw":
        return None
    return (key.build, key.backend, nodeid)
