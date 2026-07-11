# Spike: can the pytest-xdist *controller* collect first, to know the selected set before workers run?

**This is a self-contained brief for an agent with no other context. You need only `pytest` +
`pytest-xdist` in a scratch venv — no other repo.** Answer the question, with a minimal working
example, and report feasibility + cost + gotchas.

## Background (all you need)

We're building a pytest plugin that, for certain tests ("live" tests — e.g. ones needing cloud
credentials), must **fetch a credential once, up front, on the xdist controller, BEFORE the workers
fork**, then broadcast it to the workers. Fetching must be up front because it can trigger an
interactive/biometric prompt (e.g. a `1Password` CLI unlock) that has to happen at invocation time,
not minutes into a run.

The problem: we only want to fetch the credential **if a live test is actually going to run**. But
under `pytest-xdist`, **the controller process does not collect tests — the workers do, after they've
been spawned.** So at the moment we must decide "fetch or not?", the controller knows only the CLI
*arguments* (`-k`, `-m`, path args), not the actual set of selected test node-ids. Today we *predict*
reachability from the args, which has blind spots (notably `-k <expr>`, which we can't evaluate
against tests we haven't collected).

We want to know if we can do better: a true **two-phase** flow —

    (1) SCAN: controller collects → knows the exact selected node-ids
    (2) PLAN: decide which credentials/services are needed
    (3) EXECUTE: fetch/provision, then run the workers normally

## The question

**Under `pytest -n <N>` (xdist), can the CONTROLLER perform a full collection (honoring `-k`/`-m`/path
filtering) BEFORE the workers execute — so plugin code running on the controller can see the exact
list of selected test node-ids — and then let the normal xdist run proceed? At what cost and
complexity?**

## What to investigate / try

Build a tiny scratch suite (a handful of dummy tests, a couple of markers, e.g.):
```python
# test_demo.py
import pytest
def test_a(): pass
def test_b(): pass
@pytest.mark.live
def test_c_live(): pass
@pytest.mark.live
def test_d_live(): pass
```
and a plugin/conftest that tries to print, **on the controller, before any worker runs**, the exact
node-ids that WILL run — for each of:
```
pytest -n 2
pytest -n 2 -k a
pytest -n 2 -m live
pytest -n 2 -m 'not live'
pytest -n 2 test_demo.py::test_c_live
```
Concretely, explore:
1. **Does any controller-side hook run *before* workers fork AND have access to a collected+filtered
   item list?** Check `pytest_configure`, `pytest_sessionstart`, `pytest_collection`,
   `pytest_collection_finish`, `pytest_collection_modifyitems` — for each, determine (a) does it fire
   on the controller (vs only workers) under xdist, and (b) is the item list available there. (Prior
   finding: `collection_modifyitems` fires on *workers*, not the controller; `configure`/`sessionstart`
   fire on the controller but pre-collection.)
2. **Can the controller trigger its own collection on purpose?** e.g. call `session.perform_collect()`
   from a controller-side hook, or run an internal `--collect-only`-style pass, to materialize the
   filtered node-ids. Does that work under xdist without breaking the subsequent worker run? Does it
   double-collect (controller + workers)?
3. **Is there an xdist mode/hook that already collects on the controller?** (distribution modes,
   `pytest_xdist_*` hooks, `DSession` internals). Does `--dist=loadscope`/`loadgroup` change who
   collects?
4. **Cost:** if a controller collect-pass is needed, how expensive is a redundant collection (roughly
   — it'll be domain-specific, but note whether it re-imports modules, re-runs conftests, etc.)?
5. **Gotchas:** does a controller collect-pass mutate global state / warning filters / fixtures in a
   way that corrupts the real worker run? Any ordering hazards vs xdist's own setup?

## Success criteria

A **minimal working plugin/conftest** that, under `pytest -n 2 <the selections above>`, prints on the
**controller** (identify it via `not hasattr(config, "workerinput")`) the exact set of node-ids that
will run — correctly reflecting `-k` / `-m` / path filters — **before** the workers execute their
tests. If that's achievable cleanly, the two-phase design is feasible.

## Report back

- **Feasible?** yes/no, and by which mechanism (with the minimal code).
- **Cost:** does it force a redundant collection? rough overhead? any way to reuse the collection for
  the actual run instead of collecting twice?
- **Cleanliness:** any state-corruption / ordering hazards; how robust across the selection kinds
  above (especially `-k`).
- **Bottom line:** would you build the two-phase (scan→plan→execute) on this, or is predictive-from-
  args the pragmatic ceiling?

## Environment

`pytest` 9.x, `pytest-xdist` 3.8, Python 3.12+. A throwaway venv + the dummy suite above is enough —
this is pure xdist-mechanics, no domain code required.
