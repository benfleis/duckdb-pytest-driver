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
  - **TODO — nail down the "how to run" story end to end** _(2026-07-16; `uv run pytest` is now THE
    driver-dev command in AGENTS.md, but the consumer-facing story is still loose)_. Concrete threads
    surfaced hitting it live: (1) **`uvx pytest` / `uv tool run pytest` FAILS** — isolated env, no
    project, every `import ducktest` → `ModuleNotFoundError`; docs must steer people to `uv run` and
    explicitly warn off `uvx`. (2) **xdist is required to run the suite** (inner `-n 2` subprocess tests),
    now carried by the driver's `dev` dependency-group so `uv run pytest` works with no extra flag — but a
    **consumer's** extension repo needs xdist (and pyspark, connectors, …) in ITS own env/group, which the
    README's `uv run --group test pytest` implies but doesn't spell out. (3) Reconcile README (consumer
    "getting started") vs AGENTS (driver-dev loop) so they don't drift, and decide the one canonical
    consumer incantation (`uv run --group <grp> pytest`? a documented `test` group? plain venv?).
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
- **Resource-planning unification** (design proposal, validated against real code, ready to implement —
  **`docs/RESOURCE-PLANNING.md`**, rewritten as a tight PRD 2026-07-24). One canonical per-item key
  (`build, run_setting, backend, access, cell`), computed once in a new "decorate" step; batching,
  RO/RW sharing, token generation, and provisioning order all become projections/policies over that one
  key instead of independently-invented mechanisms (prompted by `to_init_sql` being the third
  near-identical gather-loop). Explicitly subsumes *Multi-service dependencies* below — read that section
  for the concrete Iceberg/azure-proxy forcing cases. 9-phase adoption path in the doc, phases 1-6 driver,
  7 driver+consumers (promote UC's already-correct `CATALOG`/`SCHEMA`/`TABLE` vocabulary), 8 az
  (`AZ_DATA_DIR`/`AZ_TEMP_DIR` → plain `DATA_DIR`/`TEMP_DIR`), 9 separate track (`.test`-file matrix
  fan-out, still blocked on Catch2's one-file-one-test model). Nothing built yet as of this note.
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
  it. Also unblocks `--repl` on a service-backed suite (same gap) — **though only the connection ENV**;
  the session still had no secret/`USE` typed for you (unlike a `@requires`-driven suite's
  `make_init_sql`). **SHIPPED 2026-07-23:** `Service`/`Credential` gained `to_init_sql(block|value, *,
  redact=False) -> str`, gathered by `--repl` (`_repl_resource_init_sql`, `plugin.py`) from every
  reachable suite's active services/credentials and prepended to a provisioner's `make_init_sql` (or
  used alone when there's no provisioner) — azurite/minio ship the worked examples. See `docs/SERVICES.md`
  § *`--repl` init SQL*. Azure uses it:
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
- **Driver owns the resource-image supply chain — ghcr as the EXCLUSIVE source** _[v0.1 BLOCKER, added
  2026-07-16]_. Distinct from the UC-server image above: this is the shared **`ducktest.resources.*`**
  emulator images (azurite, minio, and whatever comes next). Today each module pulls a third-party image
  at test time — `mcr.microsoft.com/azure-storage/azurite:latest`, `minio/minio:RELEASE.…` — which just
  bit us: `duckdb/azurite` was an intended org mirror that was never published (`repository does not
  exist`), and upstream Docker Hub/MCR bring rate limits, tag drift, and can vanish. The driver must:
  **[a] build + tag** the resource images itself (re-tag/mirror upstream azurite/minio into an
  org-controlled image; a custom build only if config isn't enough), **[b] push** them to a duckdb-org
  **ghcr** registry (`ghcr.io/duckdb/ducktest-<name>:<pinned>`; free per-run `GITHUB_TOKEN` in CI, no
  Docker Hub rate limit, org-scanned, survives upstream deletion), and **[c] use ghcr as the exclusive
  default source** — each resource module's `IMAGE` constant defaults to the pinned ghcr image, not
  upstream (env-overridable stays). This is exactly what `duckdb/azurite` *aspired* to be; the aspiration
  was right, only the build/push/publish was missing. Shares the org-namespace + CI-build decision with
  the UC-image item above; **gate before landing v0.1** (per Ben, 2026-07-16). Pairs with the live
  resource-validation tier (which then validates the ghcr images, not upstream).
  **Mechanism BUILT 2026-07-17, reworked buildx-free 2026-07-18 (namespace-agnostic, `ghcr.io/benfleis`
  interim):** `ducktest.resources._images` centralizes it; each image declares `kind` ("mirror"|"build"),
  `source`, `pin`, `platforms` via `register_image(...)` (azurite/minio are `mirror`). `ducktest
  publish-images` uses ONLY core docker — **no buildx** (buildx isn't reliably present; it was in fact
  missing on Ben's box). Two phases, both gated by `--push`:
    - per-arch push: a `mirror` copies EVERY platform from one machine (`docker pull --platform` fetches
      any arch — we never RUN it, so no QEMU — then `tag`/`push` a `…:<pin>-<arch>` slice); a `build` does
      native host-arch only (`docker build`), so run it once per arch (amd box + arm box).
    - `--finalize`: stitch the per-arch slices into the multi-arch `…:<pin>` via `docker manifest
      create`/`push`.
  This satisfies "must work by hand on an amd OR an arm box, no QEMU" (per Ben, native per-arch) and the
  "complexity absorbed by the tool, not scattered through CI/scripts" goal — CI becomes `login` + the two
  commands. **[a]+[b] done as tooling**, `kind="build"` first-class for future driver-owned build images.
  Two env knobs, nothing hard-coded: `DUCKTEST_IMAGE_NS` (namespace; default `ghcr.io/benfleis` — Ben owns
  it, no org buy-in/SSO to wait on) and `DUCKTEST_IMAGE_SOURCE` (`upstream` default -> resources still run
  third-party so the live tier stays green; `ghcr` -> serve the published image). 12 offline tests; ghcr
  auth + push verified by hand (a buildx-free smoke `pull`/`tag`/`push` to `ghcr.io/benfleis` succeeded).
  **Remaining for [c] "ghcr exclusive":** ✓(1) DONE 2026-07-18 — published `--push` + `--finalize --push`
  to `ghcr.io/benfleis`, packages **public** (anon-verified multi-arch amd64+arm64: azurite `:2026-07-17`,
  minio `:RELEASE.2025-09-07T16-13-09Z`). ✓(2) DONE — `DUCKTEST_IMAGE_SOURCE` default flipped to `ghcr` in
  `_images.py`; offline suite green (129), one stale assertion updated. ✓ live boot-from-ghcr PROVEN
  2026-07-18 — `pytest --run-docker -m docker` passes on z300 (azurite+minio boot from the public ghcr
  images with `DUCKTEST_IMAGE_SOURCE=ghcr`). (3) automate in CI — GH-hosted runners
  (decided 2026-07-18): ✓ TEST side DONE — `.github/workflows/test.yml` runs the offline suite + the
  `--run-docker` tier booting from the public ghcr images (no auth), gated by a new `ducktest pull-images`
  warm/fail-fast step. ✓ `publish-images.yml` DONE — manual
  `workflow_dispatch`, logs in with the `GHCR_PAT` secret (owner benfleis; the free `GITHUB_TOKEN` can't
  write a personal namespace) and runs the two publish commands on one runner (all mirrors). No auto-
  triggers yet (manual publish only, by choice); amd+arm matrix deferred until a `build` image exists.
  So [c] CI automation is complete; only item (4) namespace migration remains. (4) later, migrate
  `DUCKTEST_IMAGE_NS` -> `ghcr.io/duckdb` (needs the org package path cleared — checks parked with Ben).
  Azure adoption consumes the azurite image; uc/ice consume minio — why this is centralized here.

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
6. **Provisioning preflight** _(nice-to-have; consumers' CI does a blind `docker pull` today)_. A
   suite-aware command that DOWNLOADS + VALIDATES the selected run's provisioned requirements before
   the tests run — warm the served images (cf. `pull-images`), confirm creds present, probe service
   reachability — failing fast + clear. Would replace the raw `docker pull "$IMAGE"` in a consumer's
   CI (e.g. UC `integration-tests.yml`). Open: pytest-invoked (`--preflight` / collect-then-provision)
   vs CLI (`ducktest preflight -m <suite>`); overlap with `pull-images`.

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
- **`--steps` narration under `-n>1`** _(discussed, never captured until 2026-07-20)_ — today `--steps`
  (and `--repl`) hard-force `-n0` (`pytest_configure`, "single-process") because `step()` records are
  `logging` events surfaced via pytest's live-log (`log_cli`), which is controller-side: worker logs don't
  reach it under xdist, and even if they did they'd interleave incoherently across workers. The idea:
  route step begin/end events through the store — which already exists in-session for the store address +
  service env — as a small event stream (queue/broadcast), have the controller drain and render them
  serialized per-worker/per-test, so `--steps` can KEEP `-n>1` instead of collapsing to serial. Scope: a
  step-event channel on the store, worker-side `step()` publishing (in addition to / instead of the local
  log), a controller-side drainer + renderer, and dropping the `-n0` force in `_narrating`'s configure
  block (the guard already skips serial; nothing there needs changing). Note the store is per-invocation
  (not cross-process), which is fine here — this coordinates workers *within* one run, its actual purpose.
  Until then, `--steps` = serial is correct, not a bug; the help text should say "forces `-n0` (pending
  queue-forwarded narration)" so it reads as deferred, not permanent.
- **Matrix** — one body × N cells (catalog/engine), via pytest `parametrize` / scoped
  fixtures / `pytest_generate_tests`. Cells are batch-1 and likely imply `slow`. Per-cell
  variable injection into the body is the open mechanism (the default-schema trick covers many
  cases).
- **`.cpp` lane** — `--cpp` + `unittest -l` as a second gather source, deduped against the FS
  scan. Deferrable.
- **Cutover** — per-extension; `.test` files stay until each extension is stable on pytest.
- **Provisioner teardown must reclaim PHYSICAL resources, not just catalog metadata** _(cross-cutting;
  found 2026-07-17 designing iceberg rw)_. Dropping a catalog object (a `DROP TABLE` / `DROP SCHEMA
  CASCADE`) usually leaves the underlying **data files** behind, so every `rw` provision leaks storage
  that grows the warehouse/bucket each run. This is a **lakehouse-wide gap — delta, ice, uc all hit it**,
  each with its own purge verb/quirk:
  - **iceberg** (spark_local): `DROP TABLE` leaves parquet + metadata on the warehouse dir; needs
    `DROP TABLE … PURGE` (then `DROP NAMESPACE`), or rm the token'd warehouse subdir. (Wired in
    `ice/test/py/iceberg/provisioner.py teardown()` as the first instance of getting this right.)
  - **uc/databricks**: `DROP SCHEMA … CASCADE` drops managed tables + their storage, but **external**
    tables (explicit LOCATION) leave their S3 objects; the databricks provisioner's cell-schema teardown
    needs to account for external-table storage, and OSS/`uctl` similarly.
  - **delta**: `DROP TABLE` leaves the `_delta_log/` + parquet; needs the equivalent purge or a dir rm.
  The base `Provisioner.teardown()` today just drops `bindings.isolated` namespaces via `drop_sql`
  (`DROP SCHEMA … CASCADE`) — enough for pure-catalog backends, not for file-backed ones. Options: a
  base hook for "purge storage for these tables/namespaces" that each backend fills, and/or an
  offline `duck-test clean --older-than` sweep (pairs with the date-stamped provision token) for what a
  crashed run or an engine-torn-down-first teardown leaves behind. Until then, file-backed backends
  override `teardown()` per-backend (as iceberg now does) and the leak is bounded by the sweep. Ties to
  the `teardown_stale(older_than)` item in *Fixtures* below.
- **`TEMP_DIR` is the home for `rw` artifact storage — and core needs a `TEMP_DIR` vs `LOCAL_TEMP_DIR`
  split** _(design intent, 2026-07-17; the cleaner half of the reclaim item above)_.
  - **Root rw storage in `TEMP_DIR`.** An `rw` provision's *physical* storage (an iceberg warehouse dir,
    a delta table dir, a uc external-table LOCATION, an object-store prefix) should live UNDER the test's
    `TEMP_DIR` — already per-test/per-run token'd — not in a bespoke path. Then isolation and cleanup are
    a *lifecycle property* for free: `--external-test-dir-destroy on-success/always` reclaims the files,
    so the leak item above reduces to "the CATALOG object still needs its `DROP` (metadata); the FILES
    ride `TEMP_DIR`'s destroy policy" instead of a per-backend purge. Local backends (iceberg spark_local,
    delta): warehouse/table root under `TEMP_DIR`. Remote/object backends (azurite/minio/azure): the
    "`TEMP_DIR`" is a token'd bucket prefix, and pytest — which already holds the creds — `rclone`-purges
    it post-test (the object-store instantiator + a token'd prefix are what make this work). This was the
    original intent: define `TEMP_DIR` in a writeable, token'd space up front, then blow it all away.
  - **Image-config flexibility (the one real constraint).** A containerized backend (azurite / minio / uc
    server) must let its storage path be pointed at that `TEMP_DIR` space — a bind mount of the
    container's data dir to the host `TEMP_DIR` (azurite's `-l /data`, minio's data dir), or a
    configurable prefix. Not hard, but design the service images for it NOW so the storage-root model
    isn't blocked later.
  - **`TEMP_DIR` vs `LOCAL_TEMP_DIR` (a duckdb-CORE change — see *C++ queued*).** When `TEMP_DIR` is
    non-local (a remote/mounted path backing azurite/minio/…), core must ALSO allocate a `LOCAL_TEMP_DIR`
    on the local FS, with the SAME token/cleanup policies; when `TEMP_DIR` is local, the two coincide.
    Why: RW DuckDB **database** operations need real local-filesystem guarantees (file locking, mmap,
    atomic rename, fsync) that object/network storage doesn't provide — many db tests simply cannot run
    RW on non-local disk, and faking it drops exactly the durability/locking guarantees they assume. So a
    test keeps its under-test artifacts on remote `TEMP_DIR` while DuckDB's own scratch (and any local db)
    uses `LOCAL_TEMP_DIR`. The env-var contract (core-owned, duckdb `test/README.md`) grows
    `LOCAL_TEMP_DIR`; the driver/provisioner sets `TEMP_DIR` (possibly remote) and core derives the local
    sibling. Already prototyped by hand; formalize + land in core.
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
- **sqllogic failure output** _(FIXED 2026-07-14, `20268c7`)_ — a `.test` failure now surfaces the
  binary's full "Wrong result / Expected / Actual" diff. Root cause was `_classify` returning
  `data or combined`: for a query mismatch the `end`-event `data` is just the `.test:NN` location
  (truthy), so the diff in stdout got dropped. Now surfaces the binary output (`[TEST_EVENT]` lines
  stripped), falling back to `data` only when nothing was captured.
- **`provision-service` doesn't run `populate`** _[bug, found 2026-07-14]_ — the out-of-session
  `--provision-service` command calls `svc.start` directly, NOT the boot+populate factory that
  `provision_service` uses, so it leaves an EMPTY instance (azurite with no containers/data). A later
  `pytest` run repopulates on attach (masking it), but a hand-run against the provisioned instance hits
  `ContainerNotFound`. Route the command through the same one-shot `populate` so a provisioned service is
  actually ready to use.
- **Engine/connection variants + worker-lane affinity** _(designed 2026-07-15; not needed yet, kept for
  when local-Spark engines land)_. An engine that's expensive to boot and can't be shared across worker
  processes — embedded PySpark, ~10-20s for the JVM + session — becomes a **keyed resource that pins
  tests to a worker**. This is **job→worker routing (affinity), NOT `unittest` batching**; the two are
  distinct axes and this is the routing one. The driving tests are pure-Python (iceberg's spark_local,
  UC's coming cross-engine read-write tests), so they run in-worker as ordinary pytest items — there is
  **no spawned-`unittest` batch involved at all**; the only question is which worker (which already-booted
  Spark) runs a given test.

  Scoping: this need is narrow. It only arises for resources that are all three of **in-process** (not
  shareable via an endpoint), **expensive to boot**, and **variant-keyed** (mutually-exclusive configs) —
  which today is embedded Spark, essentially alone. Everything external (the OSS UC container, a SQL
  warehouse, a Spark Connect server) is shared by endpoint through the store and needs no lane pinning.
  So build this on the engine/connection abstraction, **not** as a generic affinity key on
  `@requires`/`service` — resource decls carry no scheduling hint today, and affinity would be the first,
  so keep it engine-local until a second, non-Spark case appears.

  **Variant key** = `spark_version` + runtime jars/packages + catalog config, hashed to a stable name;
  visible to the scheduler as the `xdist_group` tag (and, for out-of-process engines, as the store key on
  the endpoint block). Model on iceberg's `SparkRuntime` extended with the jar/catalog set.

  **Two mechanisms, by how the variants differ:**
  - *Same pyspark minor, different jars/catalog* → embedded, per-worker affinity. A `SparkContext` is a
    per-process singleton (jars fixed at launch, one context per JVM), so a worker hosts one variant at a
    time. Pin same-variant tests to one worker via `xdist_group=spark:<key>` (xdist loadgroup) so it boots
    the variant once and drains its lane. (iceberg's `_connection_manager` close+reboot-on-key-change is
    the un-pinned fallback, and it thrashes on interleaving.)
  - *Different pyspark minor (3.5 vs 4.0 vs 4.1)* → can't share a venv. Either **a separate invocation**
    in a separate venv (simplest; right for a small off-variant set — the suite splits at the invocation
    level) or **an out-of-process Spark Connect/Thrift server per minor**, keyed in the store, workers
    routing by variant. The latter is the only way one invocation spans minors; reserve it for when that's
    genuinely required. (Iceberg already has a small off-variant set; UC's `unitycatalog-spark_4.1` +
    `delta-spark_4.1:4.3.0` forces Spark 4.1 while iceberg is on 4.0, so a shared env would need
    convergence — see the version-collision analysis in session notes.)

  **Scheduling pressure (the real cost).** Variant affinity is a *hard* pin, not a hint: a 10-20s boot
  dwarfs load imbalance, so a worker should drain its variant lane rather than free-schedule, and a small
  lane underutilizes its pinned worker. Accept that; boot cost wins. Actionable core when picked up:
  (1) a variant key on the engine resource; (2) a collection-time step stamping `xdist_group` from that
  key (the seam that already bridges `# group:` → `xdist_group`); (3) engine-as-session-fixture that
  boots once per worker keyed by variant; (4) for the cross-minor case, the store-keyed
  Connect-server-per-variant service. Pushes on the still-open batch-ordering/affinity work and the
  engines-as-resources thread.
- **Spark connector — v1 implementation plan** _(decided 2026-07-15; the concrete build of the engine
  handle the entry above designs — v1 deliberately stays inside its "keep it engine-local" scope)_. A
  "Spark connector" is that engine handle: a session-scoped, per-worker Spark session that both the
  provisioner (to instantiate the *basis*) and a test body (to perform *mutations*) use. The critical
  path to a first working cross-engine test is Phase 0 → 1 → 2; Phase 3 and promotion are later.
  - **Phase 0 (prereq): version convergence.** Land the Spark 4.1 basis (see § *Iceberg onboarding* >
    *Spark version basis*). Everything targets Spark 4.1 + Iceberg 1.11.0.
  - **Phase 1: the handle (iceberg-local).** Formalize the stubbed `iceberg_spark_local` fixture into a
    real handle: session-scoped, boots ONE embedded `SparkSession` per worker, lazily (only if a
    test/provisioner requests it), reused across the worker's tests, `spark.stop()` at session end. A
    plain pytest fixture, NOT a ducktest `service` (embedded Spark is in-process, unshareable by endpoint
    — see the entry above). Wrap iceberg's `IcebergConnection`/`SparkRuntime` (don't reinvent); expose a
    thin surface: `.sql(stmt)`, `.rows(stmt)` (collect, for assertions), `.session` (escape hatch), and
    `.variant_key` (spark_version + jar + catalog, hashed — trivial for the one v1 variant, recorded so
    Phase 3 affinity can key on it). Replace the current `active_connection()` module-global seam with the
    handle passed to the provisioner.
  - **Phase 2: use it for both roles (re-split `schema_evolve`).** Provisioner `instantiate()` uses the
    handle to create the BASIS only (`col int` + format-v2/MoR props + 5 int rows) — ideally a `TableSpec`
    + a small Spark/Iceberg instantiator (the *Backend instantiators* item below); pragmatic v1 may keep
    the basis a trimmed iceberg-native create+insert if that instantiator isn't ready. The TEST body
    (`.py`) requests the handle and performs the mutations (`ALTER … TYPE BIGINT`; `INSERT` the bigint
    rows), then `run_paired` → DuckDB `ICEBERG_SCAN` validates the 10 rows. This is the basis-vs-actions
    split made real, and it yields the cross-engine dance for free (writer = Spark handle, reader = DuckDB
    `.test`); the reverse (DuckDB writes, Spark reads) adds a DuckDB write handle when a test needs it.
  - **Phase 3 (deferred): variant affinity + cross-minor.** Exactly the *Engine/connection variants*
    entry above (`xdist_group=spark:<variant_key>` pinning; store-keyed Spark Connect server per minor).
    v1 has one variant, so per-worker lazy boot suffices with no affinity machinery. Build only when a
    second variant actually appears.
  - **Promotion trigger.** Stays in `ice/test/py/iceberg/` until UC needs the same thing (Spark against
    the unity catalog = the second consumer), then promote to `ducktest.resources.spark` (pyspark imported
    lazily, catalog-parameterized — the azurite-resource pattern). It cannot live in the driver *core*
    (that stays pyspark-free), only in the optional resources lib. Same "local until a second consumer"
    discipline as `identity.py`→driver and `TableSource`.
- **Benchmark `solo` run-mode** — a `register_suite(..., solo=True)` (or a `benchmark` convention) that
  forces single-process / stable-timing for benchmark suites; service-backed, no creds. _(from TIERING)_
- **Shared `resources` library** — ship ready-made `service()`/`credential()` descriptors (minio /
  azurite / docker + s3 / 1Password) in `ducktest.resources` so a backend imports instead of
  re-writing them. _(from TIERING)_ **azurite + minio shipped;** a resource is only "ready-made" once it
  boots in CI, so each ships with a live-validation test — see below.
- **Live resource-validation tier** _(started 2026-07-15)_ — a shipped `resources.*` service that never
  boots in CI rots (image bump, rclone/env drift). `tests/test_resources_live.py` is the opt-in
  `docker`-marked tier: parametrized over the `service()` descriptors, it drives each through the full
  lifecycle (managed `start` → `alive` → `attach` re-probe → real rclone object round-trip → `stop` →
  container-gone) against a REAL backend. Skipped unless `docker`+`rclone` are on PATH, so the offline
  suite stays green everywhere; a new resource joins by one `pytest.param`. **Remaining:** (1) wire a
  docker-capable CI job that runs `--run-docker -m docker` (the gate before relying on these); (2) extend coverage to
  the `provision-service`/`teardown-service` CLI and the `--existing-service` attach *flag* (via
  `pytester`), plus `populate`/`to_env` adoption; (3) each new resource adds its own round-trip check.
  **First real run (2026-07-16, Ben's box) — both failures were infra/diagnosability, not test logic,
  which is the tier doing its job:** (a) **minio** booted + created the bucket fine, then the rclone probe
  upload failed `507 XMinioStorageFull` — MinIO's minimum-free-drive check tripped because `/data` was the
  container overlay on the host's near-full docker storage (no volume mounted). **Fixed 2026-07-16:**
  `_start` now mounts `/data` as a **tmpfs** (`--tmpfs /data:size=2g`, env `DUCKTEST_MINIO_TMPFS_SIZE`) —
  RAM-backed, so MinIO sees a clean sized empty drive independent of host disk, and it's ephemeral (right
  for a test emulator, torn down anyway). **Confirmed passing live (Ben's box, 2026-07-16).**
  (b) **azurite** `docker run` exited **125** (daemon-level, before the
  entrypoint) with **no visible reason** because `_docker` raises a bare `CalledProcessError` and discards
  docker's stderr — likely image `duckdb/azurite:<tag>` not pullable on that box, or ports 10000-2 taken,
  but unknowable as-is. **Fixed 2026-07-16:** `_docker` in `resources/azurite.py` / `minio.py` now raises
  with docker's stderr tail on a checked failure (mirrors the rclone `_run`), so a 125 says *why*. **Root
  cause found + fixed 2026-07-16:** the 125 was `pull access denied for duckdb/azurite, repository does not
  exist` — that mirror was never published. (az's *landed* `main` runs the azurite **npm** pkg, so no
  landed in-repo pin to source; an **image-based** azurite lives on the unlanded `benfleis/duckdb-azure`
  `convert-to-ducktest` branch — az's own ducktest adoption, blocked on landing v0.1 first — which is the
  natural place the intended azurite image + the ghcr supply chain below should be reconciled.) Repointed
  azurite at Microsoft's official
  `mcr.microsoft.com/azure-storage/azurite:latest` (public on MCR, no login). **Still TODO:** pin that to
  a verified version tag like minio's (needs a `docker pull` on a real box to read the tag). **Both
  azurite + minio now pass the live tier (Ben's box, 2026-07-16)** — the tier is real and green.
  **Code review (2026-07-16, high-effort workflow) — 6 of 7 findings fixed:** (1) the opt-in gate was
  `addopts = -m 'not docker'`, which pytest's single-valued `-m` silently REPLACES on any user `-m` (so
  `pytest -m 'not slow'` on a docker box would boot real containers) and which deselects the tier under
  path selection — replaced with a dedicated `--run-docker` flag + `pytest_collection_modifyitems` gate
  (tests/conftest.py); can't be defeated by `-m`, and shows *skipped w/ reason* instead of a silent
  deselect. (2)+(3) container leak on partial boot: the live test ran `svc.start` OUTSIDE its try/finally,
  and both `_start`s raised after `docker run` (minio's `_ensure_bucket`, azurite's readiness wait) before
  the block was stored, so `_stop_services` never reached them — start moved inside the try; both `_start`s
  now `docker rm -f` their own half-booted container on failure. (5)+(6) `_docker`/`_wait_alive` were
  duplicated verbatim across azurite/minio → extracted to `resources/_docker.py` (`docker` + `wait_until`),
  honoring the module's "mechanism in core, instance is config" contract. (7) `minio_env`/`minio_block`/
  `minio_alive` had ZERO offline coverage (azurite had a full set) → added the parallel tests to
  test_existing_services.py (guards every env key mapping + the 200-only health probe). **Not fixed —
  (4) azurite `:latest` floats:** real but already tracked above (the old `duckdb/azurite:<tag>` "pin"
  didn't exist, so it guaranteed nothing); pinning needs a tag read from a real `docker pull`, and the
  ghcr supply chain below is the durable fix. Post-fix: 113 offline pass, docker tier skips cleanly.
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
  - **Spark version basis — 4.1, not 4.0 or 4.2 (decided 2026-07-15).** Move iceberg's basis from Spark
    4.0 to **Spark 4.1** (Iceberg `1.11.0`, jar `iceberg-spark-runtime-4.1_2.13-1.11.0`). Why: 4.1 is GA
    (Spark 4.1.0, Dec 2025) with a released Iceberg runtime, AND it converges with UC/delta, which already
    force Spark 4.1 (`unitycatalog-spark_4.1`, `delta-spark_4.1:4.3.0`) — resolving the version collision
    the *Engine/connection variants* roadmap entry flags, so one env can span iceberg + UC + delta. **Do
    NOT jump to Spark 4.2** even though it GA'd 2026-07-14: Apache Iceberg ships no 4.2 runtime yet (1.11.0
    tops out at 4.1; 4.2 GA'd a day prior and Iceberg's Spark support lags), so `spark_local` can't even
    boot (`spark.jars` needs the runtime jar), and 4.2 would re-split iceberg off the 4.1 stack. Revisit
    when Iceberg ships a `4.2` runtime (~1.12.x). **4.0→4.1 tweaks:** `scripts/requirements.txt`
    `pyspark==4.0.1`→`4.1.0`; add a `"4.1"` entry to `integration_config.py`'s `SPARK_RUNTIMES` (scala
    2.13, iceberg 1.11.0) + fetch/commit the new jar; Scala 2.13.16→2.13.17 / Python min 3.10 / PyArrow
    15.0.0 are transparent; grep generator SQL for non-standard double-quote escaping (SPARK-52545
    standardized it to the SQL spec); regenerate any golden output depending on iceberg 1.10→1.11 metadata
    specifics. Java unchanged (17/21). Sources: spark.apache.org release notes 4.1.0/4.2.0; iceberg 1.11.0.
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
    OSS is separately blocked on `uctl`). **Status (2026-07-14): both halves built, code-complete.**
    Databricks: base class built, `DatabricksProvisioner` refactored onto it (verified: self-tests + a
    live dry-run smoke test reproduce pre-refactor output exactly). Iceberg: `ice/test/py/iceberg/`
    scaffolding (`iceberg_def.py`, `provisioner.py`), `ice/test/conftest.py` (registers the `iceberg_local`
    suite, `default=False` — opt-in until proven live) + `ice/test/sql/provisioned/conftest.py`
    (registers `IcebergProvisioner`, scope-limited to that subtree, mirroring UC's
    `register_provisioner(..., scope=...)` seam), and the paired starter test
    (`ice/test/sql/provisioned/schema_evolve_int_to_bigint.{py,test}`), wrapping the EXISTING generator
    def (`scripts/data_generators/tests/default/schema_evolve_int_to_bigint`) instead of reimplementing
    generation. `IcebergDef` (the lazy ref — see below) required widening `ducktest.requires.requires()`'s
    `source` validation, previously hardcoded to `Fixture`-or-string only; now any truthy object is
    accepted as a backend-defined lazy ref, **requiring an explicit `name=`** since `resolved_name()` has
    no generic way to derive a bare name from an arbitrary object (self-tested, `tests/test_requires.py`).
    **Verified without Spark** (this sandbox has no Java/PySpark — see `SANDBOX-NOTES.md`): `IcebergDef`
    resolves against the REAL `IcebergTest` registry; the full `IcebergProvisioner.provision(...,
    dry_run=True)` flow runs end-to-end (plan/tables/env all correct) against that real registry; the
    paired `.py` driver imports cleanly with the `@requires` marker correctly applied; driver self-tests
    (96 passed/5 skipped) + `ruff` clean on both repos. **Found and fixed one real bug during this**:
    `IcebergDef.resolve()`'s first draft instantiated *every* registered `IcebergTest` to find a match —
    one unrelated def (`deletion_vectors`) has a real side effect in `__init__` (opens a `duckdb`
    connection) that crashed in this environment. Fixed to match by each class's own file path
    (`inspect.getfile`, no instantiation) and only construct the actual match.
    **NOT verified — needs a real Java+PySpark+jar environment** (this sandbox has none; the
    `iceberg-spark-runtime` jar is intentionally deleted right now, see the repo-location note above):
    the live `generate()` call (does the Spark session actually boot, create the format-v2 table,
    insert, `ALTER…TYPE BIGINT`, insert again) and the final `ICEBERG_SCAN` read matching the expected 10
    rows. **Also not done, deliberately out of scope for this pass**: running `ducktest configure` for
    `ice` (would write a repo-wide `pytest.ini` — a bigger, separate decision given `ice` already has an
    established hand-rolled `test/python/conftest.py` suite predating ducktest; didn't want to change
    repo-wide pytest defaults as a side effect of one starter test).
  - **Decided:** `IcebergDef` — a new small lazy ref (mirrors `Fixture("name")`), not a plain
    FQN-string + convention lookup — keeps def-vs-fixture(-vs-IcebergDef) dispatch explicit, consistent
    with the existing lazy-ref architecture. Built in `ice/test/py/iceberg/iceberg_def.py` (extension-side,
    not the driver — it's specific to this repo's own generator registry, the same reasoning UC's
    `identity.py` lives in UC not the driver).
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

- **`LOCAL_TEMP_DIR` alongside `TEMP_DIR`** _[core, prototyped by hand]_ — when `TEMP_DIR` is non-local
  (remote/mounted backing storage), core allocates a local-FS `LOCAL_TEMP_DIR` with the same
  token/cleanup policies; when `TEMP_DIR` is local, they coincide. RW DuckDB **database** operations need
  local-FS guarantees (locking, mmap, atomic rename, fsync) object/network storage can't give, so a test
  can hold its under-test artifacts on remote `TEMP_DIR` while duckdb's own scratch uses `LOCAL_TEMP_DIR`.
  Env-var contract addition (duckdb `test/README.md`). Full rationale in the *Roadmap* item "`TEMP_DIR`
  is the home for `rw` artifact storage".
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
