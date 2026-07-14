# PLAN — roadmap, TODOs, open questions

Forward-looking only. "What is" lives in **[README.md](../README.md)** (use + integrate),
**[ARCHITECTURE.md](ARCHITECTURE.md)** (the model), and **[INTERNALS.md](INTERNALS.md)** (extending
the driver).

## Status snapshot

**Built & in use**

- pytest collector over the unittest binary: the `.test` lane (collect / batch / invoke /
  parse), xdist (`-n`, `--dist=loadgroup` + per-batch `xdist_group`), `--batch-size`.
- Skip reporting: binary emits `[DUCKDB_SKIP] <test> :: <reason>`; collector → status +
  per-reason digest (works batched).
- `--external-test-dir` + `--external-test-dir-destroy {never,on-success,always}`; per-run id
  (`timestamp--mnemonic`), cleanup-on-success.
- `--build {auto,debug,release,reldebug,relassert,latest}` binary resolution;
  `--repl` (interactive shell for one selected test) + `--provision-keep` /
  `--provision-dry-run`; `--steps` live step logging.
- SoT-by-symlink consumption (`conftest.py`, `pytest.ini`, `test/py/driver`); `@requires` +
  `resources` fixture + provisioner registry; UC scaffold (`test/py/uc`).
- Module split: `driver.sqllogic` (the `.test` lane) vs `driver.plugin` (harness / hooks).
- **Suite-based selection** — `register_suite` (path-defined groups; auto-marker so `-m <suite>` selects
  `.test` bodies too; a bare `pytest` runs the default set (smoke) + deselects the rest with a banner).
  Declared in `test/conftest.py`; vanilla repos unaffected.
- **Shared-state store** — a `multiprocessing.managers` carrier the controller starts pre-fork (workers
  connect via env); per-key **write-once/read-many state machine** (`pending → set | failed`, **poison
  pill** = waiters fail fast, no retry); spawn-safe (native socket, import-clean).
- **Up-front credentials** — `credential(...)` fetched ONCE on the controller (prompt at invocation),
  published to the store + env-adopted; a per-test **backstop** resolves store → `available()` (env,
  non-interactive) → **single-flight late-fetch** (one prompt across workers; `late_fetch=` opt-out) →
  **fail loud**. Paradigm shift: a *selected* test that can't provision FAILS, never skips.
- **Lazy services** — `service(...)` provisioned first-need via the store's single-flight (shared across
  workers), stopped once by the controller; retires the OSS filesystem lock + `reclaim_stale`.
- **UC migrated** — creds + selection + the OSS container all on the driver (`test/conftest.py`
  `register_suite`s; the hand-rolled subtree machinery deleted).

**Pending / to verify**

- **Reported-unit flip** — a driver `.py` is still the reported unit; flip so the **body**
  (`.test`) is reported and the `.py` only contributes hooks. _(pending)_
- **`--repl` python shell** — currently duckdb only; a python shell for pure-`.py` bodies is
  planned (repl-kind = body-kind). _(planned)_
- **uuid → `timestamp-mnemonic` / per-invocation → per-test** temp subdir — C++ change
  unbuilt; today the binary appends a per-invocation `<uuid>`. _(unbuilt)_
- **Python-native lane — `ducktest configure --test-kinds`** — pure-`.py` tests already collect once
  `python_files = test_*.py` is set (verified); today that's a manual `pytest.ini` edit. Add a
  `configure --test-kinds sqllogic,python[,cpp]` flag that writes the right `python_files` per declared
  lane (default sqllogic-only, keeping `python_files` empty to shield duckdb core's non-test
  `test_*.py` scripts). Record the choice (header marker) so `configure` re-runs preserve it. _(the
  py-exclusive-lane ergonomics; the lane itself works today)_
- **`ducktest` tool scope** _(decided)_ — `ducktest` stays a **config tool** (`configure`; env-agnostic,
  fine as an isolated `uv tool`). **Running is `pytest` / `uv run pytest`** — pytest + the driver plugin
  + the test deps must share one env, and `uv run` already does local-venv resolution, so don't reinvent
  it. A future `ducktest run` (if any) should be a **thin `uv run pytest` shim**, not its own env
  manager. Keep the run path clearly documented in the README.
- `${TEST_DIR_BASE}` substitutes empty in a `.test` (env-refresh timing); `__TEST_DIR__`
  (live) works — pick a live token / general var-injection channel. _(open)_
- **`.test_slow` files are silently ignored** — the collector matches the suffix `.test`
  exactly (`pytest_collect_file`: `file_path.suffix != ".test"`), so `_slow`-tagged bodies
  (`*.test_slow`, e.g. `tpch.test_slow`, `tpcds.test_slow`) are never collected or run.
  Intentional for now (keeps the slow TPC suites out of the default run), but needs fixing:
  recognize `*.test_slow` as a body too, gated behind an opt-in (a `--slow` flag and/or a
  `slow` marker so they run only when asked). _(known gap)_; additionally need to
  design/handle tags like slow

## Pre-0.1 release gates (decided 2026-07-14)

**Release plan:** push `0.0.1` to the real `duckdb`-org repo now, as-is — iterate publicly there — land
`0.1.0` after some real hardening against Iceberg (and other consumers), not before. The items below are
what "some real hardening" means concretely; treat this list, not vibes, as the 0.1.0 gate.

- Everything in **v0-dev sprints** below (collection trust is explicitly the "100% trust" blocker).
- **Reported-unit flip** and **`.test_slow`/`.test_coverage` silently ignored** (Status snapshot /
  PLAN.md:59-65) — both are silent-wrongness risks, not nice-to-haves.
- **Multi-service dependencies** (new, found 2026-07-14 pushing on Iceberg — see *Roadmap* below).
  **Still open after the azurite/service-layer commit (`f95d33b`)** — that landed `attach`/`alive` on
  `Service` (`suites.py:74-101`, the `--existing-service` mechanism, see `docs/SERVICES.md`) but nothing
  about inter-service ordering. Boot order is unenforced (composable today only by a `start()` manually
  calling `provision_service` on another descriptor) and **teardown order is worse** —
  `_stop_services` (`plugin.py:969-`) flat-loops `suite.services` in registration order, so a dependency
  can be stopped before its dependent. Iceberg's `rest` depending on `minio`
  (`ice/scripts/docker-compose.yml`) is the forcing case; **azure has a second, independent one — its
  `proxy` suite: squid (`az/scripts/run_squid.sh`) is useless until the thing it proxies (azurite) is up,
  so a proxy `.test` genuinely needs `azurite` before `squid`** (deferred with the proxy suite, but it
  means azure validates `depends_on` too, not just Iceberg). The `Service` **`depends_on` field is now
  present** (validated; `suites.py`) and **eager boot routes through `provision_service`** (so ordering
  applies for free once resolution lands). What remains: start-order **resolution** (topological,
  cycle-checked) inside `provision_service`, and **dependency-aware teardown** (`_stop_services`, reverse
  of start order — currently a flat registration-order loop). Full design in `HANDOFF-multiservice.md`
  (ducktest sandbox root, not git-tracked — fold its content in here once picked up).
- **Suite-level service eager-adoption** _(found 2026-07-14; **SHIPPED 2026-07-14** — azure-side work on
  bare `.test` conversion)_ — the service analog of `credential(adopt="env")`. A *bare* `.test` with no
  `.py` driver has no fixture to pull, so lazily-provisioned services never boot for it and its connection
  env is never set — the collector invokes the `unittest` binary directly, no Python involved. **Built:**
  `Service` gained `provision={eager,on_demand,per_test,never}` (only `eager`/`on_demand` wired;
  `per_test`/`never` named + fail-loud), `to_env(block)->dict` (env merged into `os.environ`), and
  `populate(block,config)` (structure+data, run once, must be idempotent); `use_service(base, provision=,
  to_env=, populate=)` binds a **shared** descriptor (e.g. `AZURITE_SERVICE`) to a suite's policy without
  mutating it. `_provision_eager_services` (`plugin.py`, in `_SuiteController.pytest_configure` right after
  `_fetch_credentials`, controller pre-fork) boots each reachable suite's eager services via
  `provision_service`, runs `populate` once, adopts `to_env` — so workers + the `.test` subprocess inherit
  it. Also unblocks `--repl` on a service-backed suite (same gap). Azure uses it:
  `use_service(AZURITE_SERVICE, provision="eager", to_env=azurite_env+AZ_DATA_DIR, populate=rclone-sync
  data/)`; live-verified boot + attach paths. See `docs/SERVICES.md`.
  - **`attach` × eager compose (resolved):** an `--existing-service`-attached eager service adopts
    `to_env` and runs `populate` (idempotent) but does **not** boot or teardown — env-yes / boot-no /
    seed-idempotent. (The intersection of the attach + eager paths; verified.)
  - **Reserved for `depends_on`:** the field is on `Service` now (validated), and eager boot routes
    **through `provision_service`**, so `depends_on` start-ordering will apply to eager boots for free once
    implemented; the disposition gate carries a `TODO(multiservice)` at the boot site. The remaining
    open piece is `depends_on` *resolution* + reverse-order teardown (below), driven by Iceberg.
  - **Overlaps with *Multi-service dependencies* above — same `Service` dataclass, same suite-controller
    boot hook.** `attach`/`alive` + eager-adoption are shipped; `depends_on` resolution is the last of the
    three. They compose (eager-via-`provision_service`); build `depends_on` without assuming eager-adoption
    isn't there.
  - **Explicitly does not touch the base `Provisioner`/`Bindings` work above** — different delivery
    mechanism (`os.environ` pre-subprocess vs. `Bindings.env`→`run_paired` per-test substitution) for a
    different test shape (bare `.test`, no `.py`, vs. `@requires`-driven paired tests). The
    `Provisioner` work should not assume a service became available *only* via fixture-pull, so it
    doesn't fight whatever eager-adoption path lands here.
- **UC CI isn't wired to the new suite at all yet** _(found 2026-07-14; UC-side, not driver behavior, but
  tracked here since it gates landing UC's PR)_. Verified by grepping `uc/.github/workflows/`: neither
  `LocalTesting.yml` nor `CloudTesting.yml` runs `pytest`/`ducktest` — both still call the pre-migration
  `make test_release`/`make write_tests_run`; `LocalTesting.yml` boots UC's JVM server bare on the runner
  and never touches `scripts/oss_uc_image/`. Separately, that image's build+push
  (`scripts/oss_uc_image/build_image --push`) is entirely manual today, to personal `ghcr.io/benfleis/*`
  — no `docker/login-action`, `packages: write` permission, or registry reference anywhere in CI.
  **Decided 2026-07-14 (not a landing blocker):** keep hand-running `--push` to `ghcr.io/benfleis` for
  now — ship UC's PR / this driver's `0.0.1` without waiting on this. But get the real infra —
  automated CI build+push, and the personal-vs-org-namespace call (`ghcr.io/benfleis/*` needs a stored
  PAT in org CI; `ghcr.io/duckdb/*` is clean with the free per-run `GITHUB_TOKEN` but needs org buy-in)
  — **developing in parallel**, not deferred indefinitely.

## v0-dev sprints (post-commit, near-term)

1. **Collection trust — scan-reconcile** _(the "100% trust" blocker)_. Collection is FS-only today;
   the binary's registered set (via `unittest -l`) differs — `.test_slow`/`.test_coverage`,
   `third_party/sqllogictest`, extension `_deps` are silently uncollected (false-green). Add
   `unittest -l` as a second gather source: ship **verify** (diff FS vs binary, hard-error on
   divergence) first, evolve to **authoritative** (collect from `binary ∪ FS`, dedup by name).
   Requires resolving the binary once at `pytest_configure` (see the fail-fast bug below).
2. **Slow/coverage + segmentation** _(part of the trust sprint)_. Recognize `.test_slow`/
   `.test_coverage` as bodies behind `--slow`/`slow` marker; slow tests are batch-1 → the
   batch-ordering / segmentation work lands here.
3. **Dual-scoping guard**. `testpaths` (ini, what pytest walks) vs `test_root` (plugin, what it
   accepts) can silently diverge → under/over-collection. Validate/derive at `pytest_configure`,
   error on mismatch. (FS-side half of #1.)
4. **`.sql` body lane** _(planned, marked in code)_. Extend the collector gate + `_stem_path`
   pairing once the lane is real; docstrings currently mark it "`.sql` planned".
5. **Labeled configs — `--profile`**. Named bundles (env + `--unittest-args` + provisioner
   selection) resolved above the binary; delta (minio/s3/gh-workflows) + iceberg need it. Open:
   registry home (conftest `register_profile` vs a declarative file — lean declarative for
   shareability), and profile × `@requires`/matrix interaction (a profile may pin an axis).

## Base `Provisioner` + object-store `@requires` wiring

**Status (2026-07-14): base class built + proven against Databricks.** `ducktest.provision.Provisioner`
+ `Bindings` now live in `provision.py` (exported from `ducktest`); `uc.databricks.engine.DatabricksProvisioner`
is refactored onto it (subclasses `Provisioner`, implements `before_provision`/`new_bindings`/`rw_target`/
`ro_target`/`finalize_bindings`/`env_for`/`dry_run_summary`/`instantiate`/`execute`/`teardown`/
`make_init_sql`). Verified: driver self-tests green (90 passed/5 skipped, up from 68 pre-azurite-commit —
no regressions), `ruff check` clean on both repos, and a live dry-run smoke-test (fake `rw`+`ro`
`Requirement`s through `provision()` → `make_init_sql(redact=True)`) reproduces the pre-refactor output
byte-for-byte (plan lines, `cell schema(s)`/`DEFAULT_SCHEMA` prints, `bindings.env`, generated init SQL).
`OssProvisioner` got the mechanical naming fixes only (`make_init_sql`, `teardown(bindings)` — no
internal refactor, still blocked on `uctl` separately, per below) so it stays protocol-compatible with
the shared `plugin.py` call sites without inheriting the base (duck-typed, same as before).
**Remaining: Iceberg is the second proof consumer** (not OSS — see *Iceberg onboarding* below); its
`IcebergProvisioner` hasn't been built yet.

The provisioner seam used to be *just* a protocol + registry (`register_provisioner` / `get_provisioner`);
every backend (UC OSS, Databricks) re-implemented the same spec-loop + access-policy + `Bindings` + env
assembly. That generic core is now the **base `Provisioner` + `Bindings`** below (the multi-statement
SQL-def utils already slid down to `ducktest/sqldef.py` earlier):

- **`Bindings`** dataclass: `catalog` / `default_schema` / `token`, `tables`, `isolated` (namespaces to
  drop at teardown), `env`, `plan`.
- **`Provisioner`** base: `provision(specs, token)` runs the access-policy loop — `rw` → isolated target
  + `instantiate` + track for teardown; `ro` → shared FQN + once-guard (`_shared_ro`) — then assembles
  `env` via `env_for`; `teardown` drops `isolated`. Required backend hooks: `execute(sql)`, `rw_target`,
  `ro_target`, `instantiate`, `make_init_sql`. Hooks with workable defaults: `new_bindings`, `env_for`,
  `drop_sql`, `dry_run_summary`. Two hooks the sketch below didn't anticipate but real Databricks logic
  needed: `before_provision(specs, token, dry_run)` (upfront validation/env-setup — the `--repl`-with-no-
  `@requires` guard, credential checks) and `finalize_bindings(bindings)` (post-loop bookkeeping a
  backend tracked itself during the loop, e.g. Databricks' mono-cell `default_schema` reconciliation —
  the base intentionally doesn't track per-spec state itself, that's backend-shaped).
- **Reconcile with `uc/test/py/uc/WIP-identity-design.md` § *B* before building this** — it's a richer
  design than the sketch above (which came from a session that hadn't seen it) and already informed
  today's `credential()`/`service()` shape. It splits **`Backend`/`Service`** (session-scoped —
  `start`/`stop`, `endpoint`+`execute` transport; ~5 primitives: `execute`, `instantiate_fixture`,
  `table_exists`, `attach_sql`, `catalog_for(access)` — this is now largely the azurite-commit's
  `Service`/`attach`/`alive` shape, see `docs/SERVICES.md`) from **`Provisioner`** (per-test — identity,
  lifecycle, RO presence-policy `assume`/`validate`/`provision`, `teardown_stale`). Naming fixes decided
  there: `make_init`→`make_init_sql` (still real/load-bearing for `--repl`/`--provision-dry-run`,
  `plugin.py:1263,1272` — keep it, the sketch above dropped it), `teardown(bindings)` (no redundant
  `token` arg), `sweep_stale`→`teardown_stale(older_than)`.
- **Proof consumers: Databricks + Iceberg, not Databricks + OSS.** Per `WIP-identity-design.md` §A,
  Databricks is **already fully migrated** to the unified `bindings.env` model (item 5 ✅); OSS is
  **not** — item 2 is blocked on `uctl` (hardcoded to `duck.cmt.*`/`duck.plain.*`, no dynamic
  schema/catalog creation), a container-image gap, not a refactor. So prove the base against Databricks
  (done) + Iceberg (new, see § *Iceberg onboarding* in Roadmap below), and fold OSS on once its `uctl`
  blocker clears separately — don't gate this work on that.

**Object-store `@requires` wiring rides this.** P3's rclone runner (`ducktest/tools/rclone.py`, built +
live-verified — see docs/SERVICES.md § *Object-store seeding*) is standalone today (a conftest calls
`rclone.seed(...)` directly). Route it through **`@requires` → the `Instantiator` seam** so a dataset
declared `@requires(source=…, access="ro"|"rw")` provisions via `rclone.seed(...)` and yields a **URI**
into `resources.env` — **not** a canonicalized `Table` (no duckdb middleman; opaque parquet/delta files,
per § *Fixtures* "archive/directory fixtures" below). `ro` → shared seeded prefix (seed-once / cheap
`rclone check` verify); `rw` → per-test token prefix (`container/<token>/name`, xdist-collision-free).
This object-store instantiator is the piece that plugs into the base `Provisioner` above. The
`provision-service --seed` conf-dump (SERVICES.md § P2) lands with this wiring.

## Roadmap (future)

- **Cross-process safety for `provision-service`/`teardown-service`** _(found in code review of
  `f95d33b`, 2026-07-14)_ — two related gaps, both stemming from the same root cause: no out-of-process
  ownership registry exists (`docs/SERVICES.md`'s own "deferred until needed" registry). Not fixed now —
  concurrent invocations aren't (yet) an expected usage pattern — but tracked here rather than silently:
  1. **Provision race.** `_run_service_command`'s provision path calls `svc.start(config)` directly, not
     through the store's single-flight lock — two concurrent `ducktest provision-service X` invocations
     can race on the same `docker run --name`. **This is NOT fixable by "routing through the store"**:
     `_run_service_command` runs *before* `store.start_server()` in `_SuiteController.pytest_configure`
     (the store doesn't exist yet in-process at that point), and even if it did, the store is a fresh
     per-process `multiprocessing.managers` instance (new address/authkey every run) — a separate OS
     process invoking `ducktest provision-service` gets its own independent store, not a shared one. The
     store coordinates workers *within* one pytest invocation; it cannot provide cross-process locking,
     full stop. A real fix needs a different primitive entirely — e.g. a plain file lock per service key,
     or making `start()` idempotent against a losing `docker run --name` race (catch and re-probe instead
     of crash).
  2. **Teardown-vs-running-session race.** `teardown-service`'s stop path has no ownership check — it
     will happily `svc.stop()` a service a *different*, still-running managed pytest session is using.
     No token/mechanism exists to detect this today: the block written to the store (`_service_block`:
     `{key, started}` + whatever `start()`/`attach()` add) carries no PID/run-id, and the store doesn't
     persist across processes anyway (same reasoning as #1). A real fix needs the out-of-process registry
     `SERVICES.md` already flags as deliberately deferred ("a liveness-probed on-disk record so runs
     auto-discover a running service... build it when we know we need it") — likely: record the owning
     PID/run-id in a small on-disk file per service key at boot (both the managed and `provision-service`
     paths), have teardown/stop check it and refuse (or `--force`) if the owner is still alive.
- **Matrix** — one body × N cells (catalog/engine), via pytest `parametrize` / scoped
  fixtures / `pytest_generate_tests`. Cells are batch-1 and likely imply `slow`. Per-cell
  variable injection into the body is the open mechanism (the default-schema trick covers many
  cases).
- **`.cpp` lane** — `--cpp` + `unittest -l` as a second gather source, deduped against the FS
  scan. Deferrable.
- **Cutover** — per-extension; `.test` files stay until each extension is stable on pytest.
- **Seed reuse** — standardize the OSS seed on the convention table `id_name (id INT, name
STRING)` (+ `tpc{h,ds}` for bulk reads); avoid bespoke per-test tables so provisioning stays
  one shape. (The checkpoint port reuses `id_name`.)
- **`--build auto` should fail FAST on ambiguity** _[bug]_ — with multiple binaries built (e.g.
  debug + release), `--build auto` raises `pytest.UsageError("multiple unittest binaries …
  ambiguous")` LAZILY per collection root, so pytest emits one ERROR per root (`test/common`,
  `test/extension`, `test/fuzzer`, …) and presses on instead of bailing. Resolve the binary ONCE
  at `pytest_configure` (session level) and error there, so an ambiguous / missing binary halts
  the whole session immediately with a single clear message (same for `--unittest-binary` not
  found). Binary resolution is a session invariant, not a per-item decision.
- **sqllogic failure reports should surface the binary's stdout/stderr** _[bug]_ — a `.test` failure's
  `repr_failure` doesn't include the `unittest` subprocess's raw stdout/stderr, so an unexpected
  error-in-query is opaque. Likely either the assertion-fail path doesn't thread the combined output
  through, or the `[TEST_EVENT]` data is empty for the unexpected-error-in-query case. Needs one
  instrumented run (capturing the raw subprocess output) to pin which.
- **Benchmark `solo` run-mode** — a `register_suite(..., solo=True)` (or a `benchmark` convention) that
  forces single-process / stable-timing for benchmark suites; service-backed, no creds. _(from TIERING)_
- **Shared `resources` library** — ship ready-made `service()`/`credential()` descriptors (minio /
  azurite / docker + s3 / 1Password) in `ducktest.resources` so a backend imports instead of
  re-writing them. _(from TIERING)_
- **Managed-service diagnostics — logs to the run temp dir + keep-on-failure** — a managed service is
  `docker rm -f`'d at sessionfinish **regardless of pass/fail** (`_stop_services`), so a failing run
  loses the container before you can `docker logs` it, and nothing is persisted. Generic fix (every
  managed service, not azurite-specific): where a service can emit logs, route them into the per-run temp
  dir — capture `docker logs <container>` to `BASE/<run-id>/services/<key>.log` at teardown and/or
  bind-mount a per-run host dir for the service's own debug log (e.g. `azurite -d /data/debug.log`) — and
  **retain on failure**, mirroring `--temp-dir-destroy on-success`: skip `stop` (or at least keep the
  captured logs) when the run had failures. Wants a small `service(..., logs=)`/keep-on-failure hook on
  the descriptor + `_stop_services`.
- **Declarative suite home** — allow suite/resource declaration from an ini/TOML file, not only
  `register_suite` in `test/conftest.py`. _(from TIERING)_
- **Named test sets as first-class presets (`smoke`/`local`/`cloud`/`slow`/`all`)** — today `smoke` ==
  the default-scan set (the union of `default=True` suites). But a named set won't always map to a
  high-level suite; it should be definable as an arbitrary **preset** — a marker, a `-k`-style name
  expression, an explicit test list, or a union of those — decoupled from suite `default=` flags. Design
  the declaration (e.g. conftest `register_set(name, marks=/keyword=/tests=)` or a declarative file) and
  how it composes with the default-scan + banner and with `--profile`. Turns the standard set vocabulary
  (smoke·local·cloud·slow·all) into real, composable selectors rather than just suite aliases.
- **Iceberg onboarding** — the first *external* backend to exercise the suite/resource API (today only UC
  does), validating that the surface generalizes. _(from TIERING)_ **Approach (decided 2026-07-14):** not
  a 100%-parity port — `ice`'s own team will work warts with us. Instead, a deliberately staged sequence
  of tests, each one chosen to force a specific expansion of the driver rather than exercised
  incidentally. Track each forcing case here as it's found, don't just fix it invisibly.
  - **Repo location**: `ice` moved from `d/ice` to **`ducktest/ice`** (2026-07-14, plain relocate — no
    linked worktrees, no submodule-path issues, so a straight `mv` was safe/correct here, unlike
    `driver`/`uc` which needed actual worktree splits). 4 uncommitted jar deletions in its tree
    (`scripts/data_generators/iceberg-spark-runtime-*.jar`) are intentional — large, not currently
    needed, reconstitute later — not a blocker.
  - **Starter test (decided 2026-07-14):** `schema_evolve_int_to_bigint` (def:
    `scripts/data_generators/tests/default/schema_evolve_int_to_bigint/{test.sql,__init__.py}`; read:
    `test/sql/local/schema_evolve_int_to_bigint.test`) — picked deliberately for *serious provisioning
    generation, trivial read* (multi-step Spark write: create format-v2/MOR table, insert, in-place
    `ALTER…TYPE BIGINT` schema-evolution commit, insert again; read is a flat `ICEBERG_SCAN` of one int
    column) — so any failure is unambiguously a provisioning-layer failure, not a read/assertion one.
    Close second if less schema surface wanted: `schema_evolve_widen_decimal` (same shape, decimals).
    Starts on the **`spark_local` connection (no docker)** — deliberate, matches this sandbox's own
    docker-free constraint too; REST/docker-backed catalogs (`fixture`/`spark_rest`, MinIO/S3) are a
    later step once the base below is proven.
  - **Base `Provisioner`/`Backend` design + sequencing:** see § *Base `Provisioner` + object-store
    `@requires` wiring* above (Databricks + Iceberg are the 2-consumer proof, not Databricks + OSS —
    OSS is separately blocked on `uctl`). **Status (2026-07-14): Databricks half done.** The base class
    is built and `DatabricksProvisioner` is refactored onto it (verified: self-tests + a live dry-run
    smoke test reproduce pre-refactor output exactly — see the section above for detail). **Iceberg half
    not started** — no `IcebergProvisioner`, no `ducktest/ice` scaffolding (`test/py/iceberg/`) yet. That's
    the next real task: build `IcebergProvisioner(Provisioner)` for the `spark_local` connection, wire the
    `schema_evolve_int_to_bigint` starter test through it.
  - **Open (small, low-stakes):** `IcebergDef` — a new small lazy ref (mirrors `Fixture("name")`) vs. a
    plain FQN-string + convention lookup. Leaning `IcebergDef`: keeps def-vs-fixture dispatch explicit,
    consistent with the existing lazy-ref architecture.
- **Min duckdb (unittest) version contract** _[real, mechanism TBD]_ — the driver assumes unittest flags
  (`--emit-test-events`, `--temp-dir-*`, `--select-tag`); a stale binary errors `Unrecognised token:
  --temp-dir-base`. A standalone release needs a pinned/probed floor (probe `unittest --version`/features
  or a documented minimum). _(from EXTRACT_DRIVER_PLAN)_
- **Versioning scheme** _[decided 2026-07-14]_ — see *Pre-0.1 release gates* above: `0.0.1` pushed to the
  real `duckdb`-org repo as-is, iterate publicly, `0.1.0` once hardened against Iceberg + others. Still
  open: the semver-vs-date-based policy question itself, and how API / `pytest.ini`-stub breaks get
  signaled to consumers — pairs with the min-duckdb-version contract above.
- **Scope-model unification** — map the temp-dir `create × destroy × scope` triple onto pytest fixture
  scopes AND the `@requires` resource model (one lifecycle vocabulary, not two); acquire-mode
  (shared/exclusive == ro/rw) generalizes the same triple. (Also separable: an absolute
  `$TMP/duckdb-test-temp` default base.) _(from DISPOSITIONS)_

## C++ queued (the opt-in runner changes)

- **per-test OUTCOME emit (`--emit-on-test`, its own flag)** — successor to the skip markers /
  the abandoned per-file statement histogram. Emit ONE line per Catch test-case (= per `.test`
  for sqllogic, + each C++ `TEST_CASE`): `[TEST_RESULT] <name> :: pass|fail|skip|partial[ :: <reason>]`.
  Two emit points: sqllogic already has full per-test state in `test_sqllogictest.cpp` `testRunner`
  (`error.HasError()` / `skipTestDuringRun` / `AddSkipReason`); a uniform hook for ALL tests is a
  Catch `EventListenerBase::testCaseEnded(TestCaseStats)` (none exists today; a bare listener can't
  see `partial` — that's sqllogic-layer). **Payoff:** authoritative pass/fail per test in ONE batch
  pass ⇒ driver drops the returncode + individual re-run attribution; and subsumes
  `[SKIP_TEST]`/`[SKIP_TEST_PARTIAL]`. **Outcome set is exactly {pass,fail,skip,partial}** given the
  suite. pytest mapping: pass→passed, fail→failed, skip(+reason)→`pytest.skip`, **partial→passed +
  `warnings.warn(SkipWarning)`** (there is NO native "passed-with-skip" outcome — warning is the
  idiomatic home; a custom `pytest_report_teststatus` letter is the alt). NOT available/worth it:
  xfail/xpass (Catch has `[!mayfail]`/`[!shouldfail]` but duckdb uses **0**), and `error` (a
  pytest/driver fixture-setup concept, not something the binary emits). _(deferred; ship the partial
  markers first, revisit if the batch-attribution win is worth the listener.)_
- **skip markers, done** — `[SKIP_TEST]` (whole, Catch aborts) vs `[SKIP_TEST_PARTIAL]` (region
  `mode skip`, test runs on); `PrintSkip(..., bool partial)`; driver `_scan_batch_skips` regex
  recognizes both, records `{reason, partial}`, collapses both to skip today (partial flag ready).
  On `emit-on-skip` (C++) + driver; `manage-temp-dirs` / image-tests need the restack/sync.
- per-test-stem subdir (flaggable) + a "use-my-path-as-is / don't-append-uuid" mode.
- port the SKIP marker + dir flag to core — the **only two C++ essentials** (the rest of the
  driver is moving to a SEPARATE repo, so these are the permanent core surface). Two
  independent change-sets:
  - **SKIP** — make it **opt-in** (`--emit-on-skip`, gating the shadow `SKIP_TEST` + `PrintSkip`
    → adds an arg to `unittest.cpp`) and **rename `[DUCKDB_SKIP]` → `[SKIP_TEST]`** (matches the
    Catch2 `SKIP_TEST` macro already shadowed in `runner`/`result_helper`; no other precedent in
    our/Catch2 output; generic name for the repo split). Then push. Files:
    `sqllogic_test_logger`/`runner`/`result_helper` + `unittest.cpp`. _(applied to v1.5-variegata
    + image-tests/duckdb + driver; awaiting build/verify/PR + dup to main.)_
  - **empty-list test-config quirk** _(minor, optional core fix)_ — UC sets HTTP/network errors
    fatal by overriding the binary default `skip_error_messages = {HTTP, Unable to connect}` with
    an empty set. CLI `--skip-error-messages '[]'` works; a `--test-config` JSON works too but
    ONLY as the **string** `"skip_error_messages": "[]"` — the empty **array** `[]` flattens to
    `''` in `LoadConfig` (`json->Flatten()` stringifies values) and crashes the `VARCHAR→VARCHAR[]`
    cast. Non-empty arrays are fine. UC uses the string workaround (`test/configs/uc.json`); a core
    fix would let the natural `[]` array load. No PR strictly needed.
  - **dir flag** — **HOLD.** `--external-test-dir` == create=never/destroy=never + `random`
    placement; finalize the placement set (`random`/`exact`/`stem`) + the create×destroy×scope
    contract first so the merged C++ surface is final.
- (optional) move the destroy-on-success disposition down into the binary.
- **WAIT / debugger-attach primitive** — a way to pause a specific executing test (a `.test`
  directive or runner flag: wait-on-signal / sleep / wait-for-stdin) so `lldb` can attach to
  the right unittest subprocess mid-test. Tractable because each paired test already runs in
  its own subprocess; needs to target exactly ONE test.

## Driver interface (Fork A — being designed)

- Declarative `@requires(...)` (static metadata: `source` / `access` / `properties` (an open,
  backend-interpreted dict, e.g. `commit`/`storage`) / `name`) + `@requires_matrix(...)` to fan
  a body across cells + `initialize` / `run` / `finalize` hooks + a `Context` (`spark`, `table_fq_name`,
  `table.DROP(...)`, per-cell vars). The resource model — acquire-mode × create/destroy
  disposition — see ARCHITECTURE.md § *Provisioning*.
- The `Context` surface is **not yet specified** — await the external uses doc.
- Matrix maps onto pytest parametrize; **isolation = lifecycle** (cheap / OSS) vs
  **namespace** (slow / DB), the provisioner's choice; the run-id is the shared namespace token.
- Lifecycle naming is `initialize` / `run` / `finalize` (not setup/teardown — hooks may
  assert); two layers (cell ⊃ test); ordering is LIFO.
- **Cell-schema granularity — per-RUN (documented) vs per-TEST (implemented).** The design intent was
  the run-id IS the namespace (`temp_<run-id>`, shared), but `_provision_token` appends a per-nodeid
  `sha1[:6]` → per-TEST schemas (`temp_<run-id>_<hash>`). That hash is currently **load-bearing**:
  the `@requires` lane is NOT batched (batching is sqllogic-only + size-based) and has no dep-aware
  scheduling, so the per-test name is the sole cross-worker collision guard. **Dep-aware
  provisioning** (`xdist_group`-by-dep, or a disjoint-name discipline within a run) would let the
  run-path token collapse to the documented per-run form. `--repl` needn't carry the hash
  regardless (single selected test) — a cheap, safe cleanup independent of this decision.

## Open questions

1. `Context` surface + the generator/checker/`@requires` helper-library shape.
2. Driver load timing (eager vs lazy) — gated by whether static `@requires` informs batching.
3. Per-cell variable injection into the body (matrix) — default-schema vs explicit.
4. Catalog as one matrix axis spanning managed + cloud, or separate suites (creds).
5. Cross-engine interleaved sequences: `.test`-central-with-py-steps vs py-exclusive.
6. Implicit `.py` semantics (what the do-nothing default driver provides).
7. `delta/PLAN-gen.md` — data-generation story; reconcile generators reused vs reimplemented.
8. **Tags/params in `.test` files** — pytest **markers** are the canonical select/config layer
   (`-m "slow and not flaky"`; markers double as config via `get_closest_marker`). `.py`-driven
   tests map cleanly; the open case is `.test` bodies with NO `.py` driver, which would need
   header directives (`# tags:`, more `# requires:`) bridged to markers at collection (as we
   already do for `# group:`/`xdist_group`). **Deferred on purpose:** `.test_slow` filename
   suffix is the stopgap; keep `.test` files as-is and let the real forcing function — a repo
   that declines `.py` drivers — drive adding `.test` header metadata, not imagined need.
   **Cost note (at scale):** in duckdb core (thousands of `.test` files) filtering by FILENAME
   is ~free from the FS walk, while header tags require opening + reading every file's first
   block. So filename encoding is the right lever for tags that gate COLLECTION (`slow` = don't
   even collect by default); header directives suit richer metadata read only once a file is in
   scope. Likely filename-for-collect-gates + headers-for-config, not one replacing the other.

## Fixtures — future kinds (folded from the retired FIXTURES.md)

Built today: the `Fixture` named ref (lazy, no I/O at collection); the SQL format + loader + duckdb
canonicalizer + `Instantiator` seam + `DuckDBInstantiator` + `map_columns` (see ARCHITECTURE.md §
*Fixtures*). Next:

- **Promote the table-naming contract into a driver `Provisioner` base** _(rename pending — see note)_ —
  today it's UC-local (`uc/test/py/uc/identity.py`: `TableRef` + `build_env`, ARCHITECTURE.md §
  *Provisioning*): turns a `@requires`-provisioned table into the env vars a `.test` body's `${VAR}`s
  substitute from (namespaced `{KEY}_CATALOG`/`_SCHEMA`/`_TABLE`/`{KEY}`-as-FQN per requirement, plus bare
  `{CATALOG}`/`{SCHEMA}`/`{TABLE}` for the primary). Nothing UC-specific is in it — it's already generic,
  just in the wrong repo. **Naming (flagged 2026-07-14):** the docs currently call this the "identity
  contract," which reads as auth/credentials to anyone coming in cold (confirmed — that's the first thing
  it suggested here too) when it's actually about *addressing provisioned tables*, fully disjoint from the
  credential system. Rename to something like **"table naming/addressing contract"** when this promotes —
  update `ARCHITECTURE.md` § *Provisioning*, this bullet, the module name/docstring, **and the filename
  itself** (`identity.py` → e.g. `table_naming.py` or `addressing.py` — same "reads as auth" problem
  encoded right into the path) together, not piecemeal. Also test whether the 3-field `TableRef` (catalog/schema/table) survives a non-table
  resource (a REST catalog namespace, a bucket) — Iceberg onboarding is the natural forcing case, per
  the *Pre-0.1 release gates* / Iceberg-onboarding entries above.
- **`domain=` — a shared fixture library.** A named, registered fixture root so fixtures cross repos:
  duckdb core ships a large set; an external repo submodules core (+ others) and
  `register_fixture_root(config, path, domain="core")`, then references `Fixture("t", domain="core")`.
  Default (no `domain=`) = the registering conftest's root(s), in order.
- **Explicit-kind constructors** — `Fixture.parquet(...)` (`… AS SELECT * FROM read_parquet`),
  `Fixture.gen("tpch", sf=1)` (`CALL dbgen`); bare `Fixture(name)` stays the SQL default. Each is just a
  different duckdb *body*, so canonicalization is invariant.
- **Seed sources / def-vs-data split** — generalize `.Seed()` from `None`/literal rows to a lazily
  resolved seed source: a generator (`.Seed(Gen("tpch", sf=1))`) or another fixture's rows
  (`.Seed(Fixture("big").data)` — one table *definition*, rows borrowed, structurally proving the same
  shape). Same "data producer" abstraction as `source=`; one row source plugs into either slot.
- **`Clone("cat.sch.tbl")`** — the escape hatch (CTAS from a live catalog table); the one non-self-
  contained kind, kept a separate marker (today still a bare FQN string for back-compat). Largely
  vestigial once `.gen` covers "large data self-containedly."
- **Backend instantiators / consumers** — a Spark/Databricks instantiator (type map + rows-as-VALUES
  into the existing 2×2); route the OSS + Databricks *run* paths through `resources`/`Fixture` so the
  driver `.py` collapses to one shape across backends.

### Archive / directory fixtures (a deliberate exception)

Some fixtures are a **pre-formatted directory tree** (zip/archive) a test runs against as-is — e.g.
Delta Acceptance Tests (`_delta_log/`, parquet). Self-contained, so a `Fixture` *kind*, but its
instantiation is "unpack a dir," not "make a table." Keep the kind/instantiator seam general enough
that `instantiate()` can yield **either a table (duckdb middleman) or a directory** (unpack into a
managed per-test TEMP_DIR, torn down after) — plus an optional **artifact-capture** teardown hook
(the run's output stored for an external diff) — **without** bending the common table path around this
case. Archive is the one kind that bypasses the DuckDB middleman (no schema/rows to canonicalize);
`resources` must then expose heterogeneous kinds (table → `Table`, archive → path).
