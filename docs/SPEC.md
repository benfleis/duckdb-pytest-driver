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
