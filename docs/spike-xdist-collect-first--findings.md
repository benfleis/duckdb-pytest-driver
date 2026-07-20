# Findings: can the xdist controller collect first?

Environment used: pytest 9.1.1, pytest-xdist 3.8.0, Python 3.13 (venv via `uv`,
scratch dir, no repo dependency). Scratch suite matches the brief's demo
(`test_a`, `test_b`, `test_c_live`, `test_d_live`) plus a synthetic 500-test /
10-file suite for cost measurement.

## Feasible? Yes — via `pytest_collection` (tryfirst), not `pytest_sessionstart`.

**Minimal working plugin** (conftest.py):

```python
import pytest

def is_worker(config):
    return hasattr(config, "workerinput")

def is_controller_under_xdist(config):
    return not is_worker(config) and config.option.numprocesses not in (None, 0)

@pytest.hookimpl(tryfirst=True)
def pytest_collection(session):
    config = session.config
    if not is_controller_under_xdist(config):
        return None  # not xdist, or we're a worker: let normal collection run

    items = session.perform_collect()   # honors -k / -m / path args exactly
    live_selected = [i for i in items if i.get_closest_marker("live") is not None]
    if live_selected:
        ...  # fetch credential here, before any worker has done real work
    return None  # don't return truthy: let DSession.pytest_collection still
                 # run afterward and return True (its "prohibit real collection
                 # on controller" guard) — harmless since we already collected
```

Verified against all 5 scenarios from the brief (`-n 2`, `-k a`, `-m live`,
`-m 'not live'`, and a single nodeid arg) — in every case the controller's
`perform_collect()` result exactly matches what each worker independently
collects, including the `-k` case (which predictive-from-args can't resolve).
Exit code 0 in all cases, no duplicate test execution, no crashes.

### Why not `pytest_sessionstart`? (the naive first attempt, and why it breaks)

`pytest_sessionstart` looked like the obvious hook (docs/prior-finding said it
fires pre-collection on the controller) but calling `session.perform_collect()`
from there is fragile in two distinct ways I hit in order:

1. **Default-priority hookimpl**: `session._fixturemanager` doesn't exist yet
   (it's set by `_pytest.fixtures`'s own `pytest_sessionstart`, and conftest
   hooks at normal priority run *before* that due to pluggy's LIFO-within-tier
   ordering — later-registered plugins run first). Collection raises
   `AttributeError: 'Session' object has no attribute '_fixturemanager'`.
2. **Marking it `trylast=True` doesn't fix it**: `_pytest.terminal`'s own
   `pytest_sessionstart` (which sets `self._session` on the terminal reporter)
   is *also* `trylast=True`, and conftest hooks still run before it (same LIFO
   rule applies within the trylast tier). `perform_collect()` triggers
   `pytest_deselected` → `TerminalReporter._add_stats` →
   `_is_last_item` → `assert self._session is not None` → **INTERNALERROR**,
   hard-crashing the run (not a graceful collection error — exit code 3).

Bottom line: there is no hookimpl priority in a conftest that reliably runs
"after every other plugin's sessionstart, but before DSession spawns
workers" — pluggy's ordering model doesn't give you "run dead last across all
plugins" from a conftest. `pytest_sessionstart` is the wrong hook for this.

`pytest_collection` (fired from `_main()`, strictly *after* the entire
`pytest_sessionstart` hook chain — including DSession's — has completed) is
the right one: by then every plugin's own sessionstart bookkeeping is done, so
`perform_collect()` has no missing-prerequisite hazards. The trade-off: by
this point `DSession.pytest_sessionstart` has already called
`NodeManager.setup_nodes()`, i.e. worker *processes* already exist as
execnet gateways. Empirically, though, the controller's collect-and-decide
step consistently completes and logs *before* any worker-side log line
appears (workers haven't begun their own import/collection yet) — so in
practice this is "before workers do real work," just not "before the OS
processes exist." For a credential-prompt use case that's very likely good
enough; it does not literally satisfy the "before fork" wording.

## Cost

- **Yes, it's a fully redundant collection pass** on the controller, in
  addition to the N independent collections each worker already does
  (worker-side per-worker collection is inherent to xdist and happens
  regardless of this plugin).
- Measured on 500 trivial tests / 10 files: baseline `-n 2` run ≈ 0.81s
  median of 3; with controller pre-collect ≈ 0.82s median of 3 — **~1-2%
  wall-clock overhead**. A standalone single-process `--collect-only` pass on
  the same suite costs ≈ 0.26s alone, i.e. the *CPU cost* of the extra pass is
  real (~0.26s of CPU-seconds for this suite size) but it mostly **overlaps
  wall-clock with worker startup/import** on any machine with a spare core, so
  the end-to-end run barely slows down. No way found to make workers reuse
  the controller's collection instead of redoing their own — that would
  require patching xdist's worker bootstrap (`xdist/remote.py`), not just
  plugin-level hooks.

## Cleanliness / gotchas

- **No double test execution**: confirmed by tagging every
  `pytest_runtest_logreport` — the controller *does* fire that hook once per
  test, but only because DSession relays each worker's report through the
  controller's own hook chain for reporting/aggregation; it is not a second
  real run. Don't be fooled by this when instrumenting — check
  `is_worker(config)` at the point you install the hook, not per-report.
- **Duplicate warnings**: any *import-time* warning (e.g. a deprecation
  warning at module load) gets one extra duplicate copy from the controller's
  pass, on top of the one-per-worker duplicates xdist already produces today.
  Verified: baseline `-n 2` on a module with one import-time `warnings.warn`
  already shows "2 warnings"; with controller pre-collect it becomes "3
  warnings." Cosmetic, not correctness-affecting, but worth suppressing
  (`warnings.catch_warnings()` around the manual `perform_collect()` call) if
  you don't want to double up your own plugin's warnings in the summary.
- **Robust across selection kinds**: `-k`, `-m`, bare path/nodeid args, and
  no-filter all produced exactly the same node-id set on the controller as
  workers independently arrived at, across 3 repeated runs each. `-k` (the
  documented blind spot for predict-from-args) is resolved correctly because
  this is real collection, not argument parsing.
- **Inert when not needed**: guarding on
  `not is_worker(config) and config.option.numprocesses not in (None, 0)`
  means the hook is a no-op for plain `pytest` (no `-n`) and for `-n 0`, and
  it correctly detects controller vs. worker under `-n auto` too.

## Bonus finding: a *zero-cost* controller-side hook exists, but fires later

`pytest-xdist` already has `pytest_xdist_node_collection_finished(node, ids)`
(`xdist/newhooks.py`), fired on the controller once per worker, right after
that worker finishes its own real collection
(`DSession.worker_collectionfinish` → `dsession.py:287`). This costs nothing
extra (workers collect for real regardless) and gives you the exact `ids`
list per worker with no hook-ordering hazards. The catch: it fires *after* a
worker has already spawned, imported everything, and finished collecting —
strictly later than the `pytest_collection` approach above, and per-worker
rather than a single up-front decision point. Confirmed via source read
(`dsession.py:274-293`); not separately re-tested end-to-end since the
mechanism is unconditional (doesn't branch on `--dist` mode) and the
`pytest_collection` approach already satisfies the brief's actual
requirement better. Worth knowing about if the timing constraint ever
relaxes from "before workers do anything" to "before tests are scheduled."

`DSession.pytest_collection` (`dsession.py:102-105`) unconditionally returns
`True` ("prohibit collection of test items in controller process") regardless
of `--dist` mode — confirmed by reading the source; none of
`load`/`loadscope`/`loadfile`/`loadgroup`/`worksteal`/`each` change
controller-side collection behavior.

## Bottom line

Build the two-phase design on `pytest_collection` (`tryfirst=True`),
calling `session.perform_collect()` manually and gating the credential fetch
on the result. It's feasible, correctly resolves `-k`, costs a small
(mostly-overlapped) redundant collection pass, and has no correctness
hazards beyond a cosmetic duplicate-warning artifact. It is *not* literally
"before the workers fork" (worker OS processes/execnet gateways already
exist by then) but empirically fires before any worker begins real
import/collection work, which should satisfy the actual goal (credential
ready before any live test can run). Avoid `pytest_sessionstart` — it's a
plausible-looking dead end with two independent, non-obvious ways to corrupt
or crash the run depending on hook priority. Predictive-from-args is no
longer the pragmatic ceiling; this scan→plan→execute design is worth
building.
