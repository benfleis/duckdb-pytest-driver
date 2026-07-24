"""The "decorate" step: one canonical per-item key, computed once between scan and plan.

`docs/RESOURCE-PLANNING.md` §2-3: every downstream decision (batching, resource sharing, token
generation, provisioning order) should be a projection or policy function over ONE key per test
item, instead of each mechanism inventing its own. Phase 1 (§5) lands just the data structure and
its two invocation-constant fields — the same two values `collect.py`'s `_batch_key` already
computed ad hoc, just given a name/home. Later phases extend `Key` with `backend`/`access`/`cell`
(per-test-varying fields); nothing that already reads `build`/`run_setting` needs to change shape
when that lands.
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
    """

    build: Optional[str]
    run_setting: Optional[str] = None


def decorate(item) -> Optional[Key]:
    """Compute `item`'s canonical `Key`, or None if `item` isn't a SqlLogic item (no
    `_binary`/`_working_dir` — the two attributes `SqlLogicItem`/`SqlLogicFile` stamp on collection).
    """
    build = getattr(item, "_binary", None)
    run_setting = getattr(item, "_working_dir", None)
    if build is None or run_setting is None:
        return None
    return Key(build=build, run_setting=run_setting)
