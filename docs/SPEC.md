# ducktest — Redesign Spec

*The "single good spec": the whole, written as if we'd known at the start what we learned by
iterating. Grounds a from-scratch reimplementation of the spine (leaves ported). This document is
the deliverable the reimplementation is written against.*

Status: design. Author: redesign pass, 2026-07-18. Supersedes the accreted design across
`ARCHITECTURE.md` / `INTERNALS.md` / `SERVICES.md` / `PLAN.md` (kept for history).

---

## 0. Locked constraints (not up for redesign)

- **The duckdb env-var contract stays.** `TEMP_DIR`, `{TEST_DIR}`/`__TEST_DIR__`, `.test` (SQLLogic)
  file format, the `unittest` (Catch2) binary interface. Owned by duckdb core, not us. We design to
  its edges, we don't replace it. (Edge fixes belong upstream — see §8.)
- **pytest-based, xdist-parallel.** The product IS a pytest front-end. No custom runner.
- **Python 3.12 baseline** (up to 3.14 where it pays), **uv** for envs. (Was ≥3.9.)
- **Dual-mode / import-agnostic.** Works pip-installed as `ducktest` AND vendored into duckdb core
  (`duckdb/test/py/`). Dist name (`duckdb-pytest-driver`) ≠ import name (`ducktest`). No hardcoded
  dist name in logic; relative imports; a stable public `__init__` API.
- **Spine from scratch; leaves ported.** Reimagine plugin/collection/xdist, provisioning/resources/
  store, CLI/config. Port the stable leaf utilities largely as-is: SQLLogic parse/invoke details,
  `mnemonic`, `sqldef`, `rclone`, the ghcr image supply-chain (`_images` + publish/pull).

---

## 1. Product intent (PRD)

### 1.1 What it is
Turn duckdb's `unittest` binary + its `.test` SQLLogic corpus into first-class pytest citizens:
collect `.test` files as pytest items, run them by shelling the binary with a name-filter, attribute
per-test outcomes even when many share one subprocess (batching), and layer a **declarative
provisioning** model (`@requires`, suites, services, credentials) on top so a test that needs cloud
infra just declares it.

### 1.2 The north star (the one invariant everything serves)
> **A *selected* test that cannot be provisioned FAILS — loud, red, counted. Only *deselected*
> tests are absent. "Green" means "actually ran."**

The legacy `require-env` world silently *skipped* a test whose prerequisite was missing; that made
green a lie. Every mechanism in this spec is that inversion made concrete (the store poison-pill, the
attach `alive` probe, provisioning failure → `pytest.fail`, binary-authoritative collection killing
false-green).

### 1.3 Users & jobs
- **DuckDB core / extension dev** runs `pytest` (or `uv run pytest`) in a built checkout and the
  `.test` corpus just runs, parallel, attributable. Zero conftest for the bare `.test` case.
- **Extension author** with cloud tests declares a **suite** (its services + credentials) in one
  `test/conftest.py`, writes `.test` bodies (optionally a `.py` driver), and `@requires` the tables
  a body needs. Provisioning, isolation, env delivery, teardown are the framework's job.
- **CI** calls the same commands a human does (thin CI; see the shipped ghcr workflows as the model).

### 1.4 The real developer edges the design MUST handle (enumerated — these are the spec's teeth)
1. **Bare, driverless `.test`** — pulls no fixture, so there is no natural hook to boot a service or
   set its env. (Root of the old eager/on_demand duality.)
2. **In-container-vs-host services** — Claude/pytest in a container, the service on the host: must
   *attach*, never boot or kill what it doesn't own. (Docker split-brain.)
3. **`-k` selection** — not predictable from CLI args; the historical blind spot that forced the
   backstop. (Cured by collect-first.)
4. **Interactive credential prompts** (1Password unlock/biometric) must land **once, up front, on
   the controller, before workers fork** — but only if a live test will actually run.
5. **xdist worker isolation** — session scope is *per-worker*, not per-invocation; shared infra must
   coordinate through a cross-process singleton; per-test artifacts must not collide across workers.
6. **False-green uncollected tests** — FS-only collection misses `.test_slow`/`.test_coverage`,
   `third_party/sqllogictest`, extension `_deps`; they silently don't run and nobody notices.
7. **Docker tier gating** — booting real containers must be explicit opt-in that a stray `-m` can't
   silently flip on.
8. **Config a plugin cannot own** — `addopts`/`testpaths`/`--import-mode`/`python_files` are read
   before/around plugin activation; the tool (CLI), not the plugin, must write them.
9. **Fail-fast** — an ambiguous binary (`--build auto`) or a testpaths/test_root mismatch must halt
   the session ONCE, up front, not error lazily per collection root.

---

## 2. The pytest/xdist learnings (the hard-won core, stated once)

### 2.1 The single root problem
**Under xdist the controller process does not collect tests — the workers do, after they fork.** So
at the moment we must decide *what to provision* (and whether to prompt for a credential), the
controller knows only the invocation **args** (`-k`/`-m`/paths), not the resolved node-ids. `-k
<expr>` is unresolvable from args. Everything gnarly in the old design descends from this.

### 2.2 What the old design did (and why it's a duality worth removing)
- A from-args **predictor** (`_suite_reachable`) gated both selection and eager provisioning, even
  re-implementing pytest's `-m` `Expression` engine so the two "agree by construction."
- A reactive **`pytest_runtest_setup` backstop** (store → env `available()` → single-flight
  `late_fetch` → fail) covered the `-k` case the predictor structurally cannot see.
- Env for bare `.test`s needed an **`eager` disposition** (pre-fork controller adoption) distinct
  from the **`on_demand`** fixture-pull path — two env-delivery mechanisms.
- Ordering gymnastics: a **two-plugin** split so one object is `tryfirst` (set `numprocesses`
  before xdist reads it) and another `trylast` (run after the consumer conftest registers suites).

Two mechanisms for one decision; a predictor that must mirror pytest's selection; env delivered two
ways. All symptoms of deciding *before* we know what runs.

### 2.3 The cure (proven by the spike, adopted here): collect-first
**`pytest_collection(tryfirst=True)` on the controller calls `session.perform_collect()` once,
before workers do real work.** Verified in the spike across all five selection kinds *including
`-k`*: correct, exit 0, no double execution, ~1–2% wall-clock (mostly overlapped with worker
startup). The controller now holds the **authoritative selected item set** before it decides
anything. Provisioning derives from the *real* selection, not a predictor.

**Hard rules from the spike (encode them, don't rediscover):**
- Use `pytest_collection`, **tryfirst**. NOT `pytest_sessionstart` — two independent traps: (a) at
  default priority `session._fixturemanager` doesn't exist yet → `AttributeError`; (b) even
  `trylast` can't beat `_pytest.terminal`'s sessionstart, so a manual collect's `pytest_deselected`
  → `_add_stats` → `assert self._session is not None` → INTERNALERROR (exit 3).
- Wrap the controller's collection pass in `warnings.catch_warnings()` — otherwise import-time
  warnings double-count.
- It is NOT literally "before workers fork" (execnet gateways exist), but it empirically fires
  before any worker begins import/collection — early enough for the one up-front decision.
- xdist's `pytest_xdist_node_collection_finished(node, ids)` is per-worker and too late; don't use it
  for the global decision.

### 2.4 What collect-first collapses
- **Delete** the predictive/backstop duality → one `plan` derived from the real selection.
- **Delete** `_markexpr_matches` (the re-implemented selection engine) → pytest already selected.
- **Delete** the eager/on_demand env-delivery fork → one uniform provisioning entry (§4.3).
- **Shrink** the two-plugin split: the only thing that still needs `tryfirst` is forcing
  `numprocesses`/`--dist` for `--repl`/`--steps`; that's a tiny, isolated pre-hook, not a second
  stateful controller. Suite logic runs in the collect-first phase, after collection, so no
  `trylast` dance.

### 2.5 pytest constraints that remain (must still be honored)
- **Auto-marker before builtin `-m`/`-k` deselection.** We stamp suite membership onto markerless
  `.test` bodies so `-m cloud` selects them. With collect-first WE drive `perform_collect()`, so we
  stamp markers *before* invoking it — the ordering trick becomes explicit and local (no hookwrapper
  pre-yield gymnastics needed on the controller path; workers, which still collect independently,
  keep a minimal marker-stamp).
- **`optionalhook` for xdist-absent hooks** (`pytest_configure_node`) — serial runs must not
  `PluginValidationError`.
- **Config the plugin can't own** stays in the CLI-written `pytest.ini` (§6.1).
- **`--repl`/`--steps` force `-n0`** — controller must hold items; live-log is dead on workers.
- **Batch cache coherence rests on `xdist_group` + `--dist=loadgroup`** keeping a batch on one
  worker. Keep that pin.

---

## 3. Architecture — the redesign

Nine components. The through-line: **one typed context, one collect-first phase that scans → plans →
executes, one provisioning entry.**

### 3.1 `SessionContext` (new) — kill the `config._duckdb_*` string-attribute sprawl
A single typed object built once at `pytest_configure`, stashed as `config.stash[ducktest_key]`
(pytest's typed stash, not ad-hoc string attrs). Holds session invariants: resolved binary +
`duckdb` CLI, working dir, `run_id`, the registries (suites, provisioners, instantiators — one typed
registry object, not four `_ATTR` strings), the store handle, resolved options. Everything downstream
reads the context; nothing recomputes `find_binary` per item (fixes the lazy fail-fast bug — §2.5/§9).

### 3.2 Collect-first controller: **scan → plan → execute**
The heart. On the controller, `pytest_collection(tryfirst=True)`:
1. **SCAN** — resolve the binary once (fail fast on `--build auto` ambiguity), validate
   `testpaths`⊇`test_root`, then collect authoritatively: **`unittest -l` ∪ FS**, dedup by name
   (§3.3). Stamp suite auto-markers. Call `session.perform_collect()` (wrapped in
   `catch_warnings`). Result: the true selected item set.
2. **PLAN** — from the *real* selection derive: which suites are reachable, which credentials and
   services any selected test needs, the batch assignment. One `Plan` object. No predictor.
3. **EXECUTE (provision up front)** — fetch reachable credentials (prompt once, here, pre-fork),
   provision the services the plan needs (single-flight into the store), publish blocks to the store
   + env. Then let xdist fan out; workers consume a fully-decided plan.
Serial (`-n0`) runs do the same on one process. The backstop is gone; its job (`-k`) is now just…
part of the plan.

### 3.3 Binary-authoritative collection (kills the false-green blocker structurally)
Collection has **two gather sources from day one**: the FS walk (`.test` and role-paired `.py`) and
`unittest -l` (the binary's registered set). Two modes, `--collect-source`:
- **verify** (default in CI): diff FS vs binary; **hard-error on divergence** (a `.test_slow` the FS
  gate would drop, a `third_party/sqllogictest` the FS never walked). No more silent false-green.
- **authoritative**: collect from `binary ∪ FS`, dedup by name. The binary's set is truth; FS adds
  driver pairings + non-registered bodies.
Slow/coverage bodies (`.test_slow`/`.test_coverage`) are recognized here, behind `--slow`; slow →
batch-1. `.sql` lane and Python-native lane are opt-in via `configure --test-kinds` (not a silent
suffix gate).

### 3.4 The `[TEST_EVENT]` contract (kept, hardened)
Per-test attribution inside a shared batch subprocess stays: the binary emits flare-tagged JSON
(`[TEST_EVENT] {"event":"end","name":…,"status":…}`) on stderr; the driver parses by name, not exit
code; a failed/incomplete test in a batch is **re-run individually** for a clean diff. Failure repr
surfaces the flare-stripped binary Expected/Actual (the `data or combined` fix is baked in, not a
patch). Roadmap: a real Catch2 reporter makes batch attribution authoritative — the event contract is
designed to accept that without churn.

### 3.5 The store (kept ~verbatim; promoted to public)
A `SyncManager` KV of JSON blocks with a per-key **write-once/read-many** state machine
(`absent → PENDING → SET|FAILED`) and a **poison pill** (a failed fetch fails every waiter fast — no
retry storm of prompts/boots). Verbs: `copy` (require-present) vs `copy_or_provision`
(provide-if-missing, single-flight). Spawn-safe, import-clean, JSON-blocks-unaliasable. **Change:** it
was a de-facto public dependency (drivers/tests import `ducktest.store` directly) hidden from
`__all__`; promote a curated store API into the public surface (§5).

### 3.6 Descriptors (kept): frozen, dumb, backend-agnostic
`credential`, `service`, `Suite` stay frozen dataclasses, shape-validated at construction, framework
**never imports a backend**. Keep:
- **`service()`**: `key, start, stop, attach, alive, fixture, depends_on, to_env, populate`.
- **block/derive builder contract** — a service exposes ONE `block(**overrides)` builder, never an
  exported dict; derived fields (azurite `connection_string`, minio `s3_endpoint`) recompute from
  overrides. This is the keystone that makes **boot and attach return identical shapes** — a test
  can't tell which ran.
- **boot/attach unification** behind one routing point (`provision_service`): key in
  `--existing-service` → attach (probe `alive`, populate, **never enter the store, never teardown**);
  else managed single-flight boot. Attach vs start is invisible to the test.
- **`use_service`** policy-binding via `dataclasses.replace` so a shared descriptor stays generic.

### 3.7 Provisioning: ONE entry, explicit bindings split
- **One provisioning path.** Because collect-first provisions everything the plan needs on the
  controller up front, the **eager/on_demand disposition duality is gone**. Env for a service reaches
  workers by exactly one mechanism: published to the store + adopted from it. A bare driverless
  `.test` needs no special "eager" case — its service was already provisioned in the plan. Fixtures
  still exist for `.py` drivers that want a handle, but they *consume* the already-provisioned block;
  they are not a second provisioning trigger.
- **Bindings split (fix the "generic dataclass smuggling backend data" smell).** Framework-owned
  `Bindings` = `{token, isolated, env, plan}` (the only fields the core reads/tears-down). Backend
  payload (`catalog, default_schema, tables, …`) lives in a nested `backend` field or a
  backend-defined subclass — explicitly, not smuggled through "generic" fields the base never reads.
- **Provisioner base** keeps the access-policy loop (rw → isolated namespace tracked for per-test
  teardown; ro → shared, instantiated once per session under a guard). Registered/resolved by
  test-location scope (nearest-ancestor). **Change:** `teardown()` gets a physical-storage reclaim
  hook (today it drops catalog metadata only → every `rw` leaks files; delta/ice/uc all hit it).
- **Per-test token** = `<date>_<mnemonic>_<nodeid-sha1[:6]>` (SQL-safe, age-sweepable, xdist-unique).
  **Change:** today the sha1 is a load-bearing *accident* (documented per-run, drifted to per-test to
  paper over the un-batched `@requires` lane). Here batching/affinity for the `@requires` lane is
  decided first (§3.8), and the token's uniqueness role is intentional, not compensatory.

### 3.8 Batching & affinity (decide before the token)
Contiguous same-binary/same-workdir `SqlLogicItem`s batch (size `--batch-size`), each tagged
`xdist_group("sqllogic_batch_N")` so loadgroup keeps it on one worker (batch cache stays coherent).
The `@requires` lane and matrix cells are batch-1 today; the redesign schedules them with explicit
affinity so a suite's shared `ro` provision isn't redone per worker unnecessarily — and *then* the
token is purely an isolation id, not the collision guard.

### 3.9 CLI & config (kept split; drop the argparse footgun)
- **Owned-vs-scaffold config** (`configure`) stays verbatim in spirit: `pytest.ini` is **owned**
  (byte-exact-or-fail; a hand-edit stops with a diff, never clobbered); `pyproject.toml` is
  **scaffolded** (write-once, then yours). Two dicts, two loops, independent. This is the cleanest
  surface in the codebase — keep it. **Change:** generate the README stub *from* the constant (or
  test their equivalence) so the "kept in sync by hand" liability dies.
- **Drop `argparse.REMAINDER`.** The `provision-service`/`teardown-service` `keys` + REMAINDER combo
  is a documented footgun (`-p` mis-binds to `keys`; `--` makes it *silently* worse). Replace with an
  explicit `--keys k1,k2` / `--all` and pytest passthrough after a literal `--`, parsed unambiguously
  (or move the CLI to `click`, which handles this cleanly and is already a de-facto norm).
- **publish-images / pull-images / the ghcr supply chain**: port as-is (shipped, tested, CI-proven).
- **Cross-process service ownership** (`provision-service` leaving a service running across
  invocations): today it races and can stop a live session's service, and the store *fundamentally*
  can't fix it (per-process). **Decision:** v1 ships it behind a real **on-disk lock + ownership
  registry** primitive (a file under `TEMP_DIR` keyed by service, liveness-probed), OR it's cut from
  v1 with an explicit note. Not shipped on a per-process store that can't coordinate it. (Flag as an
  open decision — §10.)

---

## 4. Module layout (new)

```
src/ducktest/
  __init__.py        public API (curated; store surface now acknowledged)
  context.py         SessionContext + the typed Registry (replaces config._duckdb_* sprawl)   [NEW]
  plugin.py          hooks only — thin; delegates to controller/collection/provisioning         [SLIMMED]
  controller.py      collect-first scan→plan→execute; the Plan object                           [NEW]
  collect.py         FS ∪ unittest -l gather, dedup, verify/authoritative modes, batching        [NEW]
  sqllogic.py        SqlLogicFile/Item, [TEST_EVENT] parse, batch invoke, individual re-run      [PORTED]
  suites.py          frozen credential/service/Suite descriptors + use_service                   [KEPT]
  store.py           write-once/read-many state machine + poison pill (public API)               [KEPT]
  provision.py       one entry; Provisioner base; Bindings (framework) + backend payload split   [RESHAPED]
  requires.py        @requires / @requires_matrix / Requirement                                  [KEPT]
  fixtures.py        TableSpec + instantiator registry (consume-only, not a 2nd provision path)  [RESHAPED]
  resources/         azurite/minio/_docker/_images (block/derive, attach, ghcr chain)            [KEPT]
  tools/             rclone (bounded verbs), and future object-store @requires bridge            [KEPT]
  cli.py             configure (owned/scaffold), service cmds (no REMAINDER), publish/pull-images [RESHAPED]
  mnemonic.py        run-ids (tests moved into tests/)                                            [PORTED]
  sqldef.py          multi-statement SQL split/load                                              [PORTED]
  steps.py           step() live-logging                                                          [KEPT]
```

Deleted vs today: the predictor (`_suite_reachable`/`_markexpr_matches`), the `pytest_runtest_setup`
credential backstop, the eager/on_demand disposition branch, the `_SuiteController` second-plugin
object, the four `config._ATTR` string attributes, the `Fixture=TableSpec` alias.

---

## 5. Public API (curated; dual-mode stable)

Exports drivers/provisioners depend on — kept stable, `Fixture` alias dropped:
- collection/run: `SqlLogicFile`, `run_paired`, `register_options`, `find_binary`, `find_duckdb`,
  `has_driver`, `is_driver`
- broadcast/context: `get_context`, `register_broadcast`, `get_broadcast`
- store (now acknowledged): `get_store`, plus a curated `store` facade (`put/copy/copy_or_provision/
  ResourceMissing/ProvisionTimeout/ProvisionFailed`) — no longer an implicit contract
- requires: `requires`, `requires_matrix`, `Requirement`, `collect_requirements`
- provision: `register_provisioner`, `get_provisioner`, `Provisioner`, `Bindings`
- suites: `register_suite`, `get_suites`, `credential`, `service`, `use_service`, `Suite`,
  `Credential`, `Service`, `provision_service`
- fixtures: `TableSpec`, `register_instantiator`, `get_instantiator`
- steps/sql: `step`, `split_statements`, `sql_literal`, `build_insert`, `run_sql_file`

---

## 6. Config the tool owns (unchanged rationale)

### 6.1 The owned `pytest.ini`
```
addopts = -n auto --dist=loadgroup --import-mode=importlib
testpaths = test
python_files =
```
Why the CLI writes it, not the plugin: pytest reads `addopts`/`testpaths`/`--import-mode`/
`python_files` before/around plugin activation. `python_files =` (empty) disables native `test_*.py`
pickup because duckdb repos ship non-test `test_*.py` scripts that `sys.exit` at import. The
Python-native lane is opt-in (`configure --test-kinds`), never a silent default.

### 6.2 Docker tier gating
`--run-docker` flag + a visible collection-time skip (with reason). NEVER `-m 'not docker'` (pytest's
`-m` is single-valued and a user `-m` silently replaces it → real containers boot unasked).

---

## 7. How-tos (developer-facing; the spec's usability test)

- **Run the corpus (bare `.test`):** build duckdb, `ducktest configure`, `pytest` (or `uv run
  pytest`). Not `uvx pytest` (isolated env → `ModuleNotFoundError`).
- **Add a suite (services + creds):** in `test/conftest.py`, `register_suite(marker="cloud",
  credentials=[credential(...)], services=[use_service(MINIO_SERVICE, provision=..., to_env=...,
  populate=...)])`. Bodies under that tree auto-carry the `cloud` marker.
- **Add a credential:** `credential(key, fetch=…, validate=…, available=…, late_fetch=…)`. Fetched
  once on the controller pre-fork if a selected test needs it; `available()` rescues preset-env.
- **`@requires` a table:** `@requires(source=TableSpec("t", ...), access="rw", name="t")` on a `.py`
  driver; the provisioner clones an isolated `rw` namespace per test (torn down after) or shares a
  session `ro` one; the body addresses `${CATALOG}.${SCHEMA}.${TABLE}`.
- **Attach to a host service (container split-brain):** `--existing-service minio` (defaults) |
  `minio=http://host:9000` | `minio={json}`; the framework probes `alive`, populates (idempotent),
  and never boots/tears-down what it attached to.
- **Docker tier:** `pytest --run-docker -m docker` (needs docker + rclone; images pull from ghcr).
- **Publish/pull images:** `ducktest publish-images --push [--finalize]`, `ducktest pull-images`.
- **Vendored into core:** same imports (relative), no dist-name assumptions; the plugin auto-registers
  via the `pytest11` entry point in either mode.

---

## 8. Delta thesis — what the single-good-spec changes (expanded in the final analysis)

**Structural wins from collect-first (scan→plan→execute):** deletes the predictor + backstop duality,
the re-implemented selection engine, the eager/on_demand env fork, the two-plugin ordering split, and
the four `config._ATTR` strings — replaced by one `SessionContext` + one `Plan`. **Binary-authoritative
collection** dissolves the "100% trust" false-green blocker at the root instead of as a bolt-on verify
pass. **Bindings split** ends backend-data smuggling. **CLI** drops the argparse REMAINDER footgun.

**What the iteration bought us that we KEEP (earned, not cruft):** the store state-machine + poison
pill; the block/derive builder contract and boot/attach unification; the `[TEST_EVENT]` per-test
attribution; the member/role model; the frozen backend-agnostic descriptors; the owned-vs-scaffold
config split; the `--run-docker` flag; scoped-by-location registries; the ghcr supply chain. These are
the parts a from-scratch author would *rediscover*, so they validate the iteration.

**What we kept that isn't worthy (drop):** `Fixture` alias; argparse REMAINDER; `store` as an
undeclared public surface; `mnemonic`'s inline non-collected tests; the manual README/const sync; the
per-test sha1 as an accidental collision guard; `provision-service`/`teardown-service` on a
coordination primitive that can't actually coordinate them.

---

## 9. Fail-fast & correctness invariants (encode as tests)
- Binary resolved **once** at configure; `--build auto` ambiguity halts the session once (not per root).
- `testpaths` ⊇ `test_root` validated at configure; mismatch errors, never silently under/over-collects.
- Collection `verify` mode hard-errors on FS-vs-binary divergence (no false-green).
- Auto-markers exist before selection deselects (pinned by a `-m <suite>`-selects-markerless-body test).
- Selected-but-unprovisionable → `pytest.fail`, counted (the north star, pinned end to end).
- Boot and attach blocks are shape-identical (pinned); attached services never enter the store.
- Store: single-flight under contention; poison-pill fails fast without retry (pinned).
- **Every scan→decorate→plan→execute boundary that carries state is an inspectable dict/struct, and
  that struct — not the eventual subprocess argv/env — is the primary unit-test surface.** (Raised
  2026-07-25, the az matrix `properties` work; the discipline this project is converging toward,
  §10 item 5's `Plan.as_dict()` being the limit case.) A matrix cell's `properties` dict (decorate-
  time), `_split_matrix_cell_properties`'s `(env, temp_roots)` tuple, and the merged `temp_roots`
  dict (execute-time) are each asserted on directly, not just inferred from a stub binary's argv —
  see `test_auto_init_sql.py`'s `_matrix_cell_env`/`_matrix_cell_temp_roots` tests. An E2E/stub-
  binary test (`test_temp_roots.py`'s pattern) still earns its keep — it's what proves the struct's
  wiring actually reaches the binary — but it's confirmatory, not where correctness of the struct
  itself gets proven; a phase's own test should never be the ONLY test of that phase's output.

## 10. Open decisions (carry into implementation)
1. **Cross-process service ownership** — ship `provision-service`/`teardown-service` on a new on-disk
   lock+registry primitive, or cut from v1? (Leaning: cut from v1, keep the in-session lazy/attach
   paths; reintroduce with a real primitive when the carrying gets annoying.)
2. **CLI framework** — stay argparse (with an explicit `--keys/--all` + `--` split) or move to
   `click`? (Leaning: click — cleaner passthrough, already a norm.)
3. **`@requires` lane batching/affinity** — the scheduling model that lets the token be a pure
   isolation id. (Needs the batch/affinity design in §3.8 pinned before the provisioner token.)
4. **Collection default mode** — `verify` everywhere, or `authoritative` once trusted?
5. **Pluggable executor / plan-as-artifact** (raised 2026-07-18, tied to the build/reassert/test/run
   script suite). Today "execute" is welded to pytest: the controller does scan+plan+provision inside
   `pytest_collection`, then the *same* process falls through to pytest's `runtestloop`; the real
   per-`.test` executor is a subprocess of the `unittest` binary; xdist workers are pytest's own
   execnet spawns. No `execv` of pytest. **Direction:** lift `execute` into an `Executor` protocol
   (`execute(plan, ctx) -> results`) selected by `--mode`, default = pytest's runtestloop, and make
   `Plan` a *serialized artifact* so it's a hand-off boundary, not just in-memory. This is the proper
   home for the modes the shipped code already special-cases via `pytest.exit` (`--repl`,
   `--provision-service`, `--steps`) and the backbone for unifying the script suite:
   one collect-first pass produces the plan, then dispatch —
   *test* = pytest loop → binary `--emit-test-events`; *reassert* = the **same binary**, expected-
   rewrite flag (a mode bit on the plan, not a new engine); *run/repl* = duckdb CLI subprocess;
   *build* = **upstream of scan** (a precondition, not a plan-executor).
   **Constraint that must ride with this decision:** selection fidelity — especially `-k` — comes from
   pytest's own collection, which is exactly what collect-first buys. So an alternate engine must
   consume a plan **produced by a collect-first pytest pass**, never re-derive selection outside
   pytest (that reinvents the predictor we deleted). A fresh `execv` of pytest is warranted only if an
   *outer* script wants a clean per-mode invocation; internally, test/reassert share the binary and
   run/repl is a CLI subprocess.
6. **`--temp-dir-base` naming/env-parity** (raised 2026-07-25, the az suite-matrix work). `--data-dir`
   has a `DUCKDB_TEST_DATA_DIR` env fallback (`test_config.cpp`'s generic `DUCKDB_TEST_*` option
   loop); `--temp-dir-base` doesn't — it's parsed directly in `unittest.cpp`'s argv loop, CLI-only.
   Also, `root` (§11.2's own term for the same thing) reads better than `base` for what's a *prefix*
   two more levels get appended onto. **Leaning:** fix both upstream (env fallback + a `root`-named
   flag, `--temp-dir-base` kept as a deprecated alias) rather than carry the asymmetry indefinitely —
   low blast radius, and it removes the CLI-arg-only reason `_matrix_cell_temp_roots` (§11.4) has to
   thread this as an argv token instead of plain env. Until it lands: `sqllogic.py`'s `_invoke` has a
   `TODO` at the `--temp-dir-base` assignment; update both there and here together.

## 11. TEMP / DATA storage — dir structure + lifecycle (authoritative)

**This section is the authority for the driver's TEMP/DATA behavior.** The binary's composition,
`LOCAL_*` resolution, and local create/sweep chain live in `test/helpers/test_config.cpp`
(`UpdateEnvironment`, `TestDirectoryPath`, `ResolveRunIdRoot`) + `test_helpers.cpp` (`PrepareTempDir`,
`DestroyTempDir`, `DestroyTestTempDir`, `ReclaimLevels`) — cite the C++, do not re-derive it. What
follows pins the **driver ↔ binary division** exactly.

### 11.1 Two axes, NOT symmetric
- **TEMP** — write scratch. Managed, per-batch, swept. Structure + lifecycle below.
- **DATA** — read-only input (fixtures). A plain path: `--data-dir` (default `working_dir/data`),
  `LOCAL_DATA_DIR = IsRemoteFile(DATA_DIR) ? working_dir/data : DATA_DIR`. **No base, no id, no per-test,
  no create/sweep, no sweeper.** The only write to a remote DATA root is out-of-band fixture
  *provisioning*, never test execution.

**Litmus: if it's swept, it's TEMP; DATA is never swept.**

**`DATA_DIR`/`TEMP_DIR` are permanently reserved names — a `.test` body can never `require-env`
either one.** Found live in the az suite-matrix work (2026-07-25): `test_sqllogictest.cpp`
unconditionally copies the binary's *entire* `test_env` map (which always has `DATA_DIR`/`TEMP_DIR`
set — override or the local-scratch default, never absent) into a fresh test's substitution scope
*before* the body parses. `require-env DATA_DIR`/`require-env TEMP_DIR` then always hits
`sqllogic_test_runner.cpp`'s "already defined" guard — **regardless of whether `--data-dir`/
`--temp-dir-base` was set**; the override changes what the names *resolve to*, not whether
`require-env` against them is legal. The fix for a `.test` file that needs these values is to use
`{DATA_DIR}`/`{TEMP_DIR}` substitution directly and drop the `require-env` gate for those two names
specifically (it's dead weight — they're never actually absent, so the gate can only ever fail, not
skip). Any *other* env var name is unaffected and `require-env`s normally.

### 11.2 TEMP directory structure
```
<root>/            outer base (default duckdb_unittest_tempdir, or a caller-set root)
  └─ <session-id>/    one pytest-driver session  (rendered as a date-sortable mnemonic — only the
       │           age-sweep cares that it sorts by date; structurally it's just an id)
       └─ <batch-id>/   one unittest invocation (a batch)
            └─ <test-id>/  one test (the binary's TEST_ID, derived from the test name)
```

### 11.3 Who creates / sweeps each level, and how it reaches the binary
`created by` / `swept by` = the **process** (driver vs binary); the middle column is the **pass-down**
mechanism (the ambiguity that bit us — it is `--temp-dir-base`, one composed string, not `--temp-dir`).

| Level | Created by | How it reaches the binary | Swept by |
|---|---|---|---|
| `<root>/` | pre-exists (neither) | inside `--temp-dir-base` | **never** (`ReclaimLevels` empty-check halts here) |
| `<session-id>/` | binary *(local, as an ancestor)* · test-on-write *(remote)* | inside `--temp-dir-base` | binary iff empty *(local; the last batch out)* · **driver** session-net, all-success *(remote)* |
| `<batch-id>/` (run-root) | binary `PrepareTempDir` *(local)* · test-on-write *(remote)* | **`--temp-dir-base = <root>/<session-id>/<batch-id>`** + `--temp-dir-run-id off` | binary `DestroyTempDir` *(local; recursive; `--temp-dir-destroy`×success)* · *(remote: none — subsumed by the `<session-id>` session net; no per-batch driver hook)* |
| `<test-id>/` | binary runner (per test) | **not passed** — the binary derives it from the test name | binary `DestroyTestTempDir` *(local; per-test success)* · **driver** session sweep, kept iff failed *(remote)* |

**One policy, two executors.** The rule is uniform: **sweep every *successful* `test-id/`; a `batch-id/`
or `session-id/` dir then falls away iff it's empty (== no failed child).** **Local** — the binary applies
it *as it goes* (per-test sweep + `ReclaimLevels` empty-prune); no driver, no rclone. **Remote** — the
driver applies it *once at session completion* with a single rclone sweep (§11.5). Same end-state; local
just gets the free incremental optimization, remote doesn't.

### 11.4 Governing flags (per invocation; not dir levels)
- **`--temp-dir-base = <root>/<session-id>/<batch-id>`** — the full per-batch run-root; the ONE composed
  string. NOT `--temp-dir` exact; NOT a `TEMP_DIR`/`LOCAL_*`/`DATA_DIR` env var (the binary
  composes/derives them and would overwrite a driver-set `TEMP_DIR`).
- **`--temp-dir-run-id off`** — run-id is redundant: the base already carries the per-batch identity, so
  no `/$RUN_ID` level is appended (`ResolveRunIdRoot` returns `$BASE`).
- **`--temp-dir-destroy {never|on-success|always}`** — gates the binary's **local** sweep only.
- **`--data-dir <dir>`** — only when overriding the read-only DATA axis; no lifecycle.

**Matrix-cell overrides of `root`/`data_dir`** (the az suite-matrix work, 2026-07-25). `_temp_roots
(config)` is cached once per `config` — session-wide, not cell-aware — but a suite-level `matrix=`
cell (two cells of the same `.test` file needing *different* remote roots, e.g. `az://` vs `abfss://`
against different storage accounts) can't share one `--temp-dir-base`/`--data-dir`. A cell declares
its need as a flat `"properties"` dict — e.g. `{"temp_dir_root": "az://acct.blob.../w", "data_dir":
"az://acct.blob.../d"}` — same spirit as `requires.py`'s `Requirement.properties`: the conftest says
*what* it needs; `sqllogic.py`'s `_split_matrix_cell_properties` is the ONE place that decides which
property names route into `temp_roots` (`temp_dir_root` → `root`, `data_dir` → `data_dir` — both
then flow through the composition above) versus passing straight through as a literal env var (any
other key). A conftest never picks CLI-arg-vs-env-var itself. `temp_dir_root` is deliberately not
named `TEMP_DIR`/`temp_dir` — it's the *prefix* `<root>` in §11.2's tree, not the composed value; a
cell is fixed at collect+decorate time (`_expand_test_matrix`), strictly before `assign_batches`
mints `<batch-id>` in the plan phase, so a cell can only ever declare the root, never the batch- or
test-id-qualified path. See `_matrix_cell_temp_roots`/`_matrix_cell_env` for the merge.

### 11.5 Execution — local (binary, as-it-goes) vs remote (driver, one sweep)
- **Local = entirely the binary, incrementally.** `DestroyTestTempDir` sweeps each passing `<test-id>/`;
  `DestroyTempDir` sweeps a passing `<batch-id>/` run-root; `ReclaimLevels` prunes empty this-run-created
  ancestors, **stopping at any non-empty (== has-a-failed-child) level** — so `<session-id>/` is reclaimed
  only by whichever batch leaves it empty. The driver never rmtrees a local level, never needs rclone.
- **Remote = entirely the driver, once at session completion** (the binary's remote clamp skips create
  AND sweep; TEMP only, **never DATA**). The driver has the failed set from the report stream (controller
  `sessionfinish`), writes those dirs as a keep-list, and runs one sweep:
  ```
  rclone delete <root>/<session-id>/ --exclude-from <keeplist>   # keeplist lines: <batch>/<test>/**
  rclone rmdirs <root>/<session-id>/                             # vacuum the now-empty passers
  ```
  Everything not under a failed dir is deleted; empty `batch-id/`/`session-id/` ancestors vacuum away; the
  failed dirs (and their non-empty ancestors) survive. (The trailing `**` is required to spare a dir's
  contents; `--exclude-if-present <marker>` is the marker-file alternative, unneeded since we have the
  list.)
- **Keep-list granularity — current compromise vs intended (both recorded).** The keep-list is only ever
  as fine as the driver can name *authoritatively* (§11.6 under-sweep). **Current:** batch granularity —
  the controller reliably maps a failed node-id → its `<batch-id>`, so it keeps `<session-id>/<batch-id>/
  **`; it does NOT reconstruct the binary's per-test `TEST_ID` leaf (reconstruct-it-wrong = over-sweep =
  forbidden). **Intended (per-test-leaf, wire later):** emit the test's temp dir (or `TEST_ID`) on a
  **failure/error** `[TEST_EVENT]` — the driver already parses that stream, so it gets the authoritative
  per-test path **in-band**: no `.failed-dirs` file, no new binary↔driver channel. That is the place to
  fix it, not unittest touching files.
- **Interruption / testing error** — a run that never reaches `sessionfinish` does no remote sweep; the
  **age-sweep** (`<root>/<old-session-id>` by date, unconditional) is the backstop so nothing leaks forever.

### 11.6 Invariants (the guardrails)
- **Under-sweep bias.** Over-sweep is the only dangerous direction (deleting a failed test's artifacts — so
  the keep-list must be authoritative/broad, never reconstructed-and-wrong); under-sweep is benign — the
  age-sweep mops it up. **When uncertain, keep.** (The binary's `ReclaimLevels` empty-check *is* this rule
  locally; the remote keep-list is its analog.)
- **Concurrency safety.** Distinct `<batch-id>` per invocation + the empty-ancestor stop-check ⇒
  concurrent batches never stomp the shared `<session-id>/`. (The earlier shared-run-root hazard is gone.)
- **One resolver of record.** The binary derives `LOCAL_*`; the driver never re-derives per-invocation
  and never sets `LOCAL_*`.
- **Remote clamp.** For a remote base the binary creates/sweeps NOTHING — the test writes, the driver sweeps.
- **Keep-on-failure by construction.** Only *failed* `test-id/` dirs are kept; their non-empty ancestors
  survive with them (empty ancestors prune). No coarse "keep the whole session on any failure" — you keep
  exactly what failed. The age-sweep is the eventual backstop, so kept-forever never happens.

This **deletes `reclaim_physical` and the old LIFO ordering** — a remote sweep is a prefix delete, so it
is order-independent. `on_cleanup` (per-`.test` SQL at test end) stays as a body-level affordance.

### 11.7 Core track (NOT the driver's job)
The binary honoring `LOCAL_TEMP_DIR` for its own spill/db is the **core** PR (`local-temp-dir` branch).
The driver introduces only: compose `<root>/<session-id>/<batch-id>` → `--temp-dir-base` (+
`--temp-dir-run-id off`, `--temp-dir-destroy`, optional `--data-dir`) → the remote TEMP sweeper. It never
re-derives `LOCAL_*`, never sweeps local, never touches DATA.

**Follow-up (core; deferred — bundled with the `TEMP_DIR`/`TEST_DIR` cleanup pass):** because the driver
now *always* passes `--temp-dir-run-id off` and bakes `<session-id>/<batch-id>` into `--temp-dir-base`,
the binary's **run-id flag machinery is dead** — `--run-id`, `--temp-dir-run-id`, and
`RUN_ID`/`ResolveRunIdRoot` no longer serve any caller and can be removed.

### 11.8 Test-kind neutrality — the pure-Python lane (intent, wire later)
The policy in §11.3–§11.6 is **executor-agnostic**. Which process runs the *local* create/sweep depends on
the test kind:
- **`.test` (SQLLogic) lane** — the **unittest binary** is the local executor (it creates/sweeps the local
  `session-id/batch-id/test-id` tree; §11.5 local bullet).
- **pure-`.py` lane** — there is no binary, so **Python (the driver) is the local executor**, applying the
  *same* policy (sweep each passing `test-id/`, prune empty ancestors, as-it-goes). Python "takes care of
  it all" here.

**Remote is always the driver** (the one rclone sweep, §11.5) regardless of lane. So the policy is uniform
across test kinds; only the *local* executor swaps (binary ↔ Python). The `.py`-lane local execution is
**not wired yet** — recorded here as intent so the model stays whole and the wiring has a home.
