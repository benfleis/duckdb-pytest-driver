# Redesign analysis — what a single good spec changes

*Answers the three questions the exercise posed: (1) what changes given one better spec than the
iteration gave us, (2) what the iteration bought that we KEEP (so the iteration wasn't waste), (3)
what we kept that isn't worthy. Grounded in the shipped code (`../driver`) vs the spec (`SPEC.md`).*

---

## The one finding that reorganizes everything

Nearly every piece of accidental complexity in the shipped driver descends from a **single root
cause**: *under xdist the controller does not collect tests — the workers do, after they fork — so at
the moment the framework must decide what to provision, it knows only the invocation args, not what
will actually run.* `-k` is the unresolvable blind spot.

From that one gap grew, independently:
- `_suite_reachable`, a **from-args predictor**, that even **re-implements pytest's `-m` `Expression`
  engine** (`_markexpr_matches`) so predicted-selection and real-selection "agree by construction";
- a reactive **`pytest_runtest_setup` credential backstop** (store → `available()` → single-flight
  `late_fetch` → fail) to catch the `-k` case the predictor structurally can't see;
- the **eager vs on_demand disposition** fork — two different ways to get a service's env to a test,
  because a bare driverless `.test` pulls no fixture so its env must be adopted pre-fork;
- the **two-plugin `_SuiteController` split** (one object `tryfirst` to set `numprocesses` before
  xdist reads it, a second `trylast` to run after the consumer conftest registered suites);
- four **`config._ATTR` string registries** and a scatter of cached invariants, read by bare string
  in a dozen sites.

A single good spec starts from the cure the team **already found and validated but never adopted**
(the xdist collect-first spike): **`pytest_collection(tryfirst)` → `session.perform_collect()`** on
the controller resolves the real selection (`-k` included) at ~1–2% overhead. Decide *after* you know
what runs, and the entire list above collapses.

---

## (1) What changes

| Shipped (accreted) | Redesign (one spec) | Why the spec version is possible |
|---|---|---|
| From-args predictor `_suite_reachable` + `_markexpr_matches` | **Deleted.** A `Plan` built from the real `perform_collect()` selection (`controller.py`, `context.Plan`) | collect-first knows the selection; no need to predict or re-implement pytest's selection |
| `pytest_runtest_setup` credential backstop | **Deleted.** Creds fetched up front from the plan's `needed_credentials` | the `-k` case the backstop existed for is just… in the plan now |
| `eager` vs `on_demand` disposition (2 env-delivery paths) | **One provisioning entry** (`controller._provision_up_front`); fixtures *consume*, don't provision | the plan provisions everything up front on the controller; a bare `.test` needs no special case |
| Two-plugin `_SuiteController` (tryfirst+trylast) | **One `Controller`** in the collect-first phase; a tiny isolated pre-hook only for `--repl`/`--steps` `-n0` forcing | suite logic runs *after* collection (in `pytest_collection`), so no `trylast` race with consumer conftests |
| 4× `config._duckdb_*` string registries + cached invariants | **One typed `SessionContext` + `Registry`** in `config.stash` (`context.py`) | nothing needs ad-hoc attrs once there's a real object; typed, single-lookup |
| FS-only collection → silent false-green (`.test_slow`, `third_party`, `_deps`) | **Binary-authoritative collect**: FS ∪ `unittest -l`, `verify` hard-errors on divergence (`collect.py`) | the binary's registered set is a gather source from day one, not a bolt-on verify pass "someday" |
| `find_binary` per collection root → lazy fail-fast bug | Binary resolved **once** at `pytest_configure` in `SessionContext` | a session invariant belongs at session scope, not item scope |
| `Bindings` smuggling `catalog`/`tables` through "generic" fields the base never reads | **Split**: framework-owned `{token, isolated, env, summary}` + explicit `backend` payload (`context.Bindings`) | the base only ever read four fields; make that the type |
| `argparse.REMAINDER` `keys` footgun (`-p` mis-binds; `--` makes it *silently* worse) | Explicit `--keys/--all` + `--`-delimited passthrough (or `click`) | the ambiguity was never necessary; it's a parser choice |
| `provision-service`/`teardown-service` on a per-process store that can't coordinate them (races, can stop a live session's service) | **Deferred to a real on-disk lock+registry, or cut from v1** (open decision) | the store fundamentally can't do cross-invocation ownership; stop pretending it can |
| `store` a de-facto public module hidden from `__all__` | **Curated store facade in the public API** | acknowledge the real contract instead of an implicit one |
| `mnemonic.py` inline non-collected tests; `Fixture=TableSpec` alias; manual README/const sync | tests → `tests/`; alias dropped; README stub generated from the constant | plain hygiene a clean start just does |

**Net structural change:** the run's control flow goes from *predict → maybe-provision → fan-out →
per-test-backstop-rescue* to a single honest **scan → plan → execute** on the controller, then a dumb
fan-out to workers that only *consume*. The line count of `plugin.py` drops hard (the predictor,
backstop, `_markexpr_matches`, the second plugin object, the `_split` selection mirror all leave); the
concepts a new contributor must hold drops harder.

---

## (2) What the iteration bought us — KEEP (the iteration was not waste)

These are the parts a from-scratch author would **rediscover**, which is the strongest possible
validation that the iteration found something real, not incidental:

- **The store as a write-once/read-many state machine with a poison pill** (`store.py`). Single-flight
  provisioning + fail-fast-for-all-waiters is exactly the right primitive for controller/worker
  sharing of creds and service blocks. Kept verbatim; only *promoted* to public.
- **The block/derive builder contract** (a service exposes one `block(**overrides)`, never a dict;
  derived fields recompute) → **boot and attach return identical shapes**, so a test can't tell which
  ran. This is the keystone that makes in-container-vs-host transparent. Non-obvious, hard-won, kept.
- **Boot/attach unification behind one routing point** — attach never enters the store, so controller
  teardown naturally leaves host-owned services alone. The generic answer to docker split-brain.
- **The `[TEST_EVENT]` flare-tagged per-test event stream** — per-test attribution inside a shared
  batch subprocess, with individual re-run only on failure for a clean diff. The right binary↔driver
  contract; kept and designed to accept a real Catch2 reporter later.
- **The member/role model** ("roles are not file types": a `.py` can be driver, body, or both). Clean,
  minimal, extensible. Kept in `collect.py`.
- **The frozen, backend-agnostic descriptor layer** (`credential`/`service`/`Suite`, shape-validated
  at construction, framework never imports a backend). Excellent separation; kept.
- **Scoped-by-test-location registries** (nearest-ancestor resolution) so a mixed two-extension run
  doesn't let a global last-wins clobber. Kept as the ONE `Registry` idiom (was three copies).
- **The owned-vs-scaffold config split** (`configure`: `pytest.ini` byte-exact-or-diff, `pyproject`
  write-once). The cleanest surface in the codebase; kept nearly verbatim.
- **The `--run-docker` flag over `-m 'not docker'`** (pytest's `-m` is single-valued and silently
  replaceable). Sound reasoning, kept.
- **The north star itself** — "selected-but-unprovisionable fails loud; green means ran." The
  paradigm inversion is the product's soul; the redesign makes it *more* true (binary-authoritative
  collection extends "loud" to uncollected tests).
- **The ghcr supply chain** (buildx-free publish/pull, namespace-agnostic, the mirror-never-runs-so-
  no-QEMU insight). Shipped, CI-proven; ported unchanged.

The pattern: the iteration's **data-model and contract discoveries** (store semantics, block/derive,
event stream, descriptors, roles) are all keepers. What iteration produced that a spec avoids is the
**control-flow scaffolding** built to cope with deciding-before-knowing.

---

## (3) What we kept that isn't worthy — DROP

- **The predictor + backstop duality** — two mechanisms for one decision, only because the real
  selection wasn't available at decision time. Collect-first makes both unnecessary.
- **`_markexpr_matches`** — re-implementing pytest's own selection engine to make a prediction match
  reality. Pure symptom.
- **The `eager`/`on_demand` disposition fork** — the single most re-explained concept in the docs
  (~4 places), a sharp edge that exists only because bare `.test`s pull no fixture. One provisioning
  entry deletes the distinction.
- **The two-plugin `_SuiteController` split** — elegant *given* the tryfirst-vs-trylast constraint,
  but the constraint itself is an artifact.
- **Four `config._ATTR` string registries** — implicit data-flow; replaced by one typed context.
- **`argparse.REMAINDER` for service-command args** — a documented footgun where `--` makes it
  *silently* worse. Never necessary.
- **`store` as an undeclared public surface** — an implicit contract; either acknowledge it or hide
  it, don't leave it ambiguous.
- **The per-test `sha1` token doing double duty as the `@requires`-lane collision guard** — a
  load-bearing *accident* (documented per-run, drifted to per-test to paper over an un-batched lane).
  Decide batching/affinity first; then the token is a pure isolation id, intentionally.
- **`provision-service`/`teardown-service` on a coordination primitive that can't coordinate them**
  — ships a race. Either give it a real primitive or cut it.
- **Hygiene cruft**: `Fixture=TableSpec` alias, `mnemonic` inline non-collected tests, hand-synced
  README/const, the `skip-mode`→`warn_explicit` and `data or combined` classify workarounds (the
  latter obsoleted by a proper per-test result emit).

---

## What this says about iterating vs. specifying

The iteration was **right to happen** — it discovered the store semantics, the block/derive contract,
the event stream, the descriptor separation, and the north star, none of which are obvious a priori.
A team could not have specified those without building toward them.

What a single good spec buys is **not the discoveries — it's the absence of the scaffolding built
around a decision made too early.** Given foreknowledge of the collect-first result, you never build
the predictor, the backstop, the disposition fork, or the two-plugin split; you build scan→plan→
execute once. The redesign keeps ~100% of the iteration's *contracts* and deletes ~100% of its
*coping mechanisms*. That is the honest measure of what a better spec would have been worth: not a
different product, a much smaller one.
