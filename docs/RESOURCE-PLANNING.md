# Resource planning — a unified sharing-scope model (PROPOSAL, not built)

> **Status: design proposal.** Nothing in this doc is implemented. It exists to pin down a target shape
> before the next few resource-provisioning features (matrix-aware backends, attach-only cloud
> resources, cross-resource `depends_on`) get built as three more one-off, parallel mechanisms instead of
> one generalization. See `ARCHITECTURE.md` (the model as built today) and `PLAN.md` (the roadmap this
> feeds into) for what's actually live.

## Why this doc exists

Three times now, the driver has solved "a resource is shared by more than one test; provision it once,
tear it down once, don't redo the work" — and gotten a slightly different answer each time:

1. **Credentials/services** (`Credential`/`Service`, `suites.py`): shared **once per invocation**, via the
   store's cross-process single-flight (`PENDING → SET | FAILED`). Genuinely necessary here — two workers
   racing `docker run --name X` actually conflicts.
2. **RO tables** (`Provisioner._shared_ro`, `provision.py:100-114`): shared **once per worker process** —
   a plain in-memory `set()` on the (per-worker) Provisioner instance. Weaker than (1), and — as far as
   we can tell — not because RO instantiation *needs* to be weaker (it's typically an idempotent
   `CREATE ... IF NOT EXISTS`, safe to redundantly run), but because this code predates the store's
   generalization and was never revisited.
3. **`--repl` init SQL** (`_repl_resource_init_sql`, `plugin.py`, 2026-07-23): a *third*, structurally
   near-identical "gather across reachable suites, dedup by key" loop, written because the first two
   didn't compose into something this could reuse.

Meanwhile, RW tables get a *third kind* of scope for free: per-test isolation via the `token` (optionally
folded with a matrix cell — see `uc`'s `cell_schema_name(commit, storage, token)`,
`test/py/uc/databricks/engine.py:122`).

None of these are wrong in isolation. But every new cross-cutting mechanism (this doc is prompted by
three concrete upcoming ones — a real backend matrix, attach-only cloud resources, and multi-service
ordering) is at risk of becoming a *fourth* parallel implementation of "shared resource, some scope,
provision-before-first-use, teardown-after-last-use" instead of one.

## Target use cases

These are the concrete cases the model below is scoped against — not hypotheticals:

- **`--repl` works on azurite** (shipped 2026-07-23, `to_init_sql` — see `SERVICES.md`). The first case;
  this doc generalizes past it, doesn't redo it.
- **RO/RW sharing is inherent, but the *scope* and *key* need fixing.** RW's per-test isolation via
  `token` is right. RO's per-worker sharing is (probably) accidentally weaker than it needs to be, and —
  more importantly — its dedup key isn't matrix-cell-aware anywhere today (below).
- **Azure's 3-way backend matrix**: one test body (`azure.test`) needs to run against
  `{azure-az, azure-abfss, azurite-az}`. Each cell materializes to a secret + a `DATA_DIR`/`TEMP_DIR` pair:
  two cells point at **pre-provisioned, externally-owned** real Azure paths
  (`az://<real-blob-account>/<fixed-data-path>`, `abfss://<real-adls-account>/<fixed-data-path>`, plus a
  writable temp prefix under each) that this driver never boots or tears down; the third
  (`azurite-az`) is **locally booted, driver-owned, ephemeral** (boot, populate, teardown — the existing
  `AZURITE_SERVICE` shape). Same test body, same abstract resource *shape*, three genuinely different
  lifecycle owners.
- **httpfs's matrix** (not yet built here): `{minio-bucket, a public read-only https server}`, plus real
  S3/R2/GCS variants. Same shape as azure's: a mix of locally-booted emulators and pre-provisioned
  external endpoints sharing one test body.
- **UC (Databricks/OSS)**: Databricks is already on the unified `Bindings`/`env` model (credential +
  RO/RW tables, no matrix today). OSS is blocked on a separate `uctl` tooling gap, not a design gap.
- **Iceberg**: `rest` depends on `minio` being up first (`Service.depends_on` exists, validated, but
  start-order resolution + reverse-order teardown aren't implemented yet — `PLAN.md` § *Multi-service
  dependencies*). This is the same "provision before first use, teardown after last use" principle,
  scoped (so far) to only two services.

Concurrency is explicitly **not** the goal of this phase — everything the driver runs today is linear
per worker anyway. The goal is **consistent correctness** (a resource dedup key that can't silently
collapse two different things, a lifecycle model that doesn't special-case "pre-provisioned, no
teardown" as an afterthought). If the scope/key/`TEMP_DIR` model ends up right, concurrency becomes
close to free later — it's a side effect worth naming, not a design driver now.

## The gap, precisely

Three concrete problems, not one vague "unify everything":

### 1. Sharing scope is implicit in *which code path a resource happens to go through*, not a declared property

A resource's actual lifecycle guarantee (per-invocation single-flight vs. per-worker best-effort vs.
per-test-isolated) is currently a consequence of *which of the two systems* (`Service`/`Credential` vs.
`Provisioner`) it was written against — not something stated once and enforced uniformly. There's no
single place that says "this resource is invocation-global" the way there's no single place that says
"this resource is per-test-isolated" — you find out by reading which mechanism happened to implement it.

### 2. The RO dedup key isn't matrix-cell-aware, and nothing enforces that it should be

`Provisioner.ro_target(spec, state)`'s return value **is** the `_shared_ro` dedup key
(`provision.py:108-114`). Today's only real consumer (`uc`'s Databricks `ro_target`,
`engine.py:313-320`) never folds cell/`params` identity into it — because Databricks' RO sources are
static premade FQNs, not backend-varying. That's an accident of what's been tried, not evidence the key
doesn't need cell-awareness: the azure 3-way matrix is exactly the case where it would — `azure.test`'s
`@requires`-equivalent resource resolves to a *different account* per cell, sharing one nominal source.
Without an explicit, enforced rule that the dedup key is a function of `(resolved identity, cell)`, a
backend can silently under-share (redo work) or — worse — over-share (cell B's lookup satisfied by cell
A's wrong-backend target) purely by omission.

### 3. There's no resource shape for "externally owned, no managed lifecycle at all"

`service()` requires `start` (`suites.py` — "Only `start` is required" per `SERVICES.md`). A
pre-provisioned real Azure/S3 account has no boot and no teardown — it just exists, permanently, outside
this driver's control. Today that's not a first-class resource; it'd have to be smuggled in as constant
env or a service with a `start` that's never meant to be called (relying on every consumer always
declaring it `--existing-service`, which is exactly the kind of implicit contract this doc is trying to
get rid of).

### 4. Batching and resource-sharing already compute "what can this item share" independently, for what's the same question

`collect.py`'s `assign_batches`/`_batch_key` (`collect.py:182-187`) already computes a key —
`(binary, working_dir)` — to decide which items may share **one `unittest -f filelist` subprocess call**.
That's a *different mechanism*, computed by *unrelated code*, answering the *same shape of question* as
gap #2's resource-sharing key: "what can this item share with that item." Nothing connects them today.
Concretely, that's a real hazard, not just an aesthetic one: `_batch_key` doesn't (and can't yet) include
resource/matrix-cell identity, so nothing structurally prevents two items that *must* stay isolated (two
different matrix cells of what will eventually be a fanned `.test` file; today, `cli_auth.test`-style
env-sensitive tests sharing a worker with an unrelated cloud test) from landing in the same batch/worker
purely because their `(binary, working_dir)` happen to match. §*The unifying primitive*, below, is the
fix for gaps #2 and #4 together, not two separate fixes.

## The unifying primitive: one key per item, computed once ("decorate")

Read scan → collect-first plan → execute (`ARCHITECTURE.md` § *xdist model*) as three phases today:
**scan** (collect every item, `.py` and `.test` alike), **plan** (`Controller.pytest_collection`
resolves reachability, provisions services/credentials up front), **execute** (run each item, batched or
not). Insert one more, between scan and plan: **decorate** — compute exactly ONE canonical key per item,
before anything gets scheduled or provisioned. Everything downstream (batching, resource sharing, token
generation) becomes a *projection or policy over that one key*, not a separate mechanism:

```
key(item) = (binary, working_dir, config_variant, resource_identity, cell)
```

- `binary` / `working_dir` — already computed today, `_batch_key`'s two components.
- `config_variant` — **new**: a named run-level axis (e.g. httpfs's `{curl, httplib,
  connection_caching, dynamic}` — see § *A different "matrix"*, below) once it's declared as a small set
  of named variants instead of an ambient global CLI flag. Until declared, this component is constant
  for a whole invocation, same as today.
- `resource_identity` / `cell` — the `@requires`/`@requires_matrix` resolved source + per-cell
  properties that already exist for `.py` tests today; the not-yet-built equivalent for bare `.test`
  files once matrix fan-out lands.

**Everything else is a policy function *over* this key, not an independently-invented mechanism:**

- **Batching equivalence** = items may share one subprocess call iff they project equal on
  `(binary, working_dir, config_variant)`. Today's `_batch_key` is exactly this projection with
  `config_variant` fixed (it doesn't exist yet) — extending it later is additive, not a rewrite.
  **Critically, `resource_identity`/`cell` must stay OUT of the batching-equivalence projection on
  purpose** — not because it doesn't matter, but because two different cells can never share one
  subprocess call *anyway* (Catch2's one-file-one-registration wall, § *What this doc does not solve*).
  Once cell-fan-out exists, cell identity has to additionally *exclude* co-batching across cells — the
  projection stays a *subset* of the full key, deliberately, to prevent exactly the gap #4 hazard.
- **RW isolation token** = a deterministic value derived from the **full** key (`access="rw"`) — this is
  what `_provision_token`/`cell_schema_name(commit, storage, token)` already hand-compute today (an
  ad hoc `<date>_<mnemonic>_<nodeid-hash>` format); the proposal is that the token simply **is** a
  canonical encoding of the key, not a separately-invented string that happens to also be unique.
- **RO shared-target identity** = a deterministic value derived from the key **projected onto
  `(resolved source identity, cell)`** — dropping `binary`/`working_dir`/`config_variant`. Whether that
  projection is always correct (should an RO table really be shared across two different `config_variant`s?)
  is a real, backend-dependent question — flagged in *Open questions*, not assumed away.
- **Provisioning schedule** for invocation-scoped resources (services, credentials, `invocation-external`
  paths) projects onto just the resource-identity component — a docker container doesn't care which
  matrix cell or build variant is asking for it.

This is deliberately still "collect-first": decorating (computing keys) is pure — no side effects, no
scheduling decisions, just data. A separate graph/scheduler stage consumes the decorated keys to build
the actual plan (batch assignment, provision-before-first-use/teardown-after-last-use ordering, resource
dedup). Policy (which projection, which hash) is applied *before* the graph processor runs, not folded
into it — the graph processor still just executes a plan built from already-computed keys, same
separation of concerns the collect-first redesign already established at a coarser grain.

## Proposed model

### A resource node has an explicit sharing scope

Four scopes, not two:

| scope | lifecycle | today's analog | dedup mechanism |
|---|---|---|---|
| **invocation-managed** | boot once, teardown once, whole run | `Service` (managed) | store single-flight |
| **invocation-attached** | no boot, no teardown, just `attach`+`alive` once | `Service` (`--existing-service`) | store, present but inert |
| **invocation-external** | no boot, no teardown, **no attach probe either** — just a location + a credential, assumed always there | *(none today — the gap)* | none needed; it's a constant |
| **per-test-isolated** | instantiate per `(token, cell)`, teardown per test | `Provisioner` rw | token (+ cell) in the target name |
| **per-invocation-shared** | instantiate once per `(resolved identity, cell)`, never torn down mid-run | `Provisioner` ro (currently per-*worker*, see below) | store single-flight, keyed by `(identity, cell)` |

`invocation-external` is the new one the azure/httpfs matrices need for their pre-provisioned cells — a
resource that is neither managed nor attach-probed, just a fixed fact (an account name, a fixed
`DATA_DIR`, a credential to reach it). It's simpler than a `Service`, not a variant of one; forcing it
through `service()`'s `start`-required shape would be the wrong direction.

### RO gets promoted from per-worker to per-invocation, using the mechanism that already exists

`_shared_ro` becomes a store-backed single-flight, the same primitive services/credentials already use —
not a new mechanism, just RO stopping being the one resource class that doesn't get it. This is
behavior-preserving for every current caller (idempotent `CREATE`s stay idempotent; they just happen
once instead of once-per-worker) and is the concrete "consistency now, concurrency for free later" case:
removing N redundant `CREATE`s across workers is exactly the kind of win the store was built for.

**One nuance this needs, found by checking it against Iceberg's real `IcebergProvisioner`**
(`ice/test/py/iceberg/provisioner.py`): its RO tables are generated through `iceberg_spark_local`, an
*embedded* PySpark session that's deliberately **not** a driver `Service` — the code says so explicitly:
"each xdist worker gets its own embedded session" (`provisioner.py:51`), because there's no cheap way to
share a live JVM object across OS processes. At first glance that looks like a case where per-worker
sharing is *inherent*, not an accident to fix — a real crack in "just promote everything to the store."
It isn't, once two questions get separated:

- **"Can this work be skipped because it's already done?"** — invocation-wide, store-coordinated, same
  as any other RO resource. Iceberg's actual generated *data* lands in a fixed warehouse path
  (`_SPARK_LOCAL_WAREHOUSE`) that's the same for every worker on the machine — nothing about *that* is
  worker-bound.
- **"Who actually performs the work, if it does need doing?"** — worker-owned, and sometimes genuinely
  can't be anything else (you can't hand a live PySpark session to another process). That's fine: the
  first worker to win the store's single-flight race runs `generate()` on *its own* connection, writes to
  the shared path, and marks the store `SET`; every other worker sees `SET` and never calls `instantiate()`
  at all — it just uses what got written.

So the promotion still holds; the model just needs to say this explicitly, because "the resource can be
shared" and "the connection that touches it can be shared" are different claims, and conflating them is
exactly what would make Iceberg look like an exception when it isn't one.

### The dedup/sharing key becomes an explicit, first-class input — never an implicit side effect

See § *The unifying primitive*, above: the RO/RW dedup key stops being "whatever string a backend's
`ro_target`/`to_init_sql`/etc. happened to return" and becomes an explicit projection of the one
canonical per-item key. A backend still owns how *identity* resolves (that part is correctly
backend-specific — an FQN, an account name, whatever); what stops being backend-discretionary is
*whether cell identity folds into sharing* — that's a property of the projection, applied uniformly,
not something a backend can silently get wrong by omission.

### Scheduling: one topological rule, not one per resource kind

"Provision before first use, teardown after last use" — generalized past the two-service Iceberg case
(`rest`/`minio`) to the full resource-node set (`depends_on` already exists on `Service`; the missing
piece is resolution + reverse-order teardown, tracked in `PLAN.md`). Once every resource — service,
credential, RO table, invocation-external path — is a node in the same graph, this is one scheduler, not
bespoke ordering logic per resource kind.

## How this maps onto the target use cases

- **`--repl`/azurite**: unaffected — already an `invocation-managed` service; `to_init_sql` stays as-is.
- **RO/RW + matrix**: RW's `token`(+cell) isolation is already correct, unchanged. RO moves to
  `per-invocation-shared` (store-backed) with an explicit `(identity, cell)` key — closes gap #2 and #1
  above for the one consumer (Databricks) that has RO today, and makes the key contract explicit before
  a second RO consumer (azure) needs it.
- **Azure 3-way matrix**: `azurite-az` is `invocation-managed` (today's `AZURITE_SERVICE`, unchanged).
  `azure-az`/`azure-abfss` become `invocation-external` nodes — an account name, a fixed `DATA_DIR`, and
  the `azure_spn` credential (already planned, see the earlier azure-conversion conversation) reached via
  `to_init_sql`. Per-cell `TEMP_DIR` still needs *something* under it to own cleanup of what a test
  writes — for pre-provisioned accounts that's a path-prefix sweep (the same mechanism `TEMP_DIR`/remote
  reap already uses for other resources, per `SPEC.md` §11), not a container teardown. Matrix fan-out
  itself (turning one `azure.test` into three pytest-visible items) is a **separate, still-open**
  problem — see the note below; this doc unifies *what gets provisioned per cell*, not *how a `.test`
  file becomes N items*.

  **`DATA_DIR`/`TEMP_DIR` are the delivery mechanism, and they already exist** — a per-item key resolves
  to a set of values (which account, which data path, which temp prefix); `DATA_DIR`/`TEMP_DIR` are
  exactly the existing, already-built channel those values reach a test through, per cell, no new
  plumbing needed. The one real check, done against a live file (`test/azurite/azure.test`): its body
  hardcodes the literal `testing-private` instead of substituting `{AZ_DATA_DIR}` — so it isn't
  matrix-ready as written, while the already-migrated cloud tests (`test/azure/basic.test` and siblings)
  already use the placeholder correctly. That's a small, mechanical rewrite per file where it's missing,
  not a gap in the model — most bodies already follow the convention; this is confirmation the model
  composes with what's already there, not a new problem to solve.
- **httpfs matrix**: same shape as azure's — extra evidence this generalizes rather than being
  azure-specific.
- **UC/Iceberg**: Databricks already conforms (no change). Iceberg's `rest`→`minio` ordering is exactly
  the topological-scheduling piece above, generalized rather than reimplemented per resource kind. OSS UC's
  `uctl` blocker is unrelated infrastructure, out of scope here. **Caveat, found during the
  categorization pass**: Iceberg's `minio`/`rest` are still a raw `docker-compose.yml`
  (`ice/scripts/docker-compose.yml`), not yet declared as driver `Service`s at all — so the
  `depends_on` ordering case is still only a plan, not something checked against real code yet.

## A different "matrix" that looks similar but isn't: whole-invocation config sweeps

httpfs's CI (`IntegrationTests.yml`) runs its **entire suite four times**, each under a different
`--test-config` (`httpfs_{dynamic,curl,httplib,connection_caching}.json`). Three of those vary
`httpfs_client_implementation`/`httpfs_connection_caching` — plain `DBConfig`-scoped `SET` options
(`src/httpfs_extension.cpp:132-172`, `config.SetHTTPUtil(...)`), cheap to flip at runtime, per the
extension's own comment ("HTTP util classes are supposed to be cheap … don't store resources"). The
fourth varies `statically_loaded_extensions` (compiled-in vs. `LOAD`-at-runtime), a build/linkage
assumption.

This is **not** a resource-node matrix in this doc's sense: there's no service, credential, or table
identity involved, no per-test lifecycle, nothing to provision or tear down — it's a single
whole-**invocation** `DBConfig`/`on_init` knob applied uniformly to every test in one `unittest` run.
Ducktest already generalizes this correctly, today, with no new mechanism: `--unittest-args` is
deliberately whole-invocation-scoped (threaded into every batched `unittest` call uniformly —
`resolve_unittest_args`, `sqllogic.py:230`, whose own docstring uses `--test-config x.json` as its
example). The right shape stays "run `pytest --unittest-args='--test-config …json'` N times" — that's
not a workaround for a driver limitation, it's the correct fit for what this axis actually is. Naming it
here only to keep it from getting conflated with the per-test backend matrix above in a future reader's
mental model — they look superficially similar ("a matrix of test configs") but are different in kind.

**If this axis were ever declared as a named `config_variant`** (§ *The unifying primitive*) instead of
staying an ambient global CLI flag, worker-level partitioning of the sweep — running all four configs
*concurrently* in one `pytest -n 4` session instead of four sequential full-suite passes — falls out of
the *same* batching-equivalence projection, for free: items with different `config_variant` values
already wouldn't batch together (different projection), so nothing new has to be built to keep them on
separate workers, they just naturally don't co-batch. Worth remembering if this sweep ever becomes worth
parallelizing; not a reason to build it now.

## What this doc does *not* solve

**Fanning a bare `.test` file into N matrix-cell pytest items is still blocked by Catch2's registration
model**, independent of everything above (see the earlier conversation this session: one `.test` file is
one registered Catch2 test case, so two cells of the same file can never share one subprocess
invocation — each cell needs its own `unittest`/shell invocation). This doc makes *what a cell needs
provisioned* uniform and correct; it doesn't remove the need for N separate invocations for a fanned
`.test` file. Whether that's solved by file generation (a template stamped into N `.test` files, one per
cell — the pragmatic answer today) or by teaching the collector a native fan-out is a separate design
question, deliberately out of scope here.

## Incremental adoption (no big-bang rewrite)

1. **(pure refactor, no behavior change)** Introduce the canonical per-item key as a real data structure,
   computed once in a "decorate" step between scan and plan. Initially it carries exactly what
   `_batch_key` carries today (`binary`, `working_dir`) — this step is just giving the existing thing a
   name and a home, not changing what it does.
2. **(mechanical, low-risk)** Dedupe the reachable-suite gather loop itself — `provision_reachable` and
   `_repl_resource_init_sql` become one shared "for each reachable suite's active resource, call `f`"
   helper. No behavior change.
3. **(behavior-preserving hardening)** Promote `_shared_ro` from a per-worker `set()` to a store-backed
   single-flight. Existing callers unaffected; redundant cross-worker instantiation goes away.
4. **(real API addition)** Extend the key with `resource_identity`/`cell`, and make RW-token generation
   and RO-shared-identity both explicit *policy functions over a projection of the key* — replacing
   today's independently-hand-rolled `_provision_token`/`cell_schema_name`/`ro_target`-as-key. This is
   the first change that touches the `Provisioner` contract every consumer implements.
5. **(new capability)** Add `invocation-external` as a resource kind — no `start` required, just a fixed
   block/value + optional credential. This is what azure's two pre-provisioned cells and httpfs's public
   read-only server actually need.
6. **(scheduling)** Resolve `depends_on` (topological start order + reverse teardown) across the unified
   resource-node set — subsumes Iceberg's `rest`/`minio` case for free once it's not scoped to services
   only.
7. **(optional, only if it becomes worth it)** Declare `config_variant` as a named axis (httpfs's
   http-client sweep) and fold it into the key — unlocks worker-partitioned concurrent sweeps for free
   via the existing batching-equivalence projection (step 1), per § *A different "matrix"* above. Not
   currently justified by need; listed for completeness.
8. **(separate track)** `.test`-file matrix fan-out mechanics (file generation vs. a native collector
   concept) — deliberately decoupled from 1-7; can proceed independently once resource provisioning
   itself is uniform. **Prerequisite from step 1's key design, though**: batching equivalence must
   exclude `resource_identity`/`cell` from its projection *before* fan-out lands, or two cells of a
   fanned file can silently co-batch (gap #4) the moment fan-out is turned on.

## Open questions

- Is `@requires_matrix`'s per-cell `properties` dict the right vehicle to carry the cell key into
  `invocation-external`/RO dedup, or does a bare-`.test`-oriented equivalent (no Python, no `@requires`)
  need its own, parallel cell-identity concept?
- For `invocation-external` resources, who owns `TEMP_DIR` cleanup semantics when the resource is a
  pre-provisioned real-cloud path rather than a driver-managed container — is the existing remote-reap
  sweep (`SPEC.md` §11) already sufficient, or does a pre-provisioned path need a narrower/scoped prefix
  convention to avoid the driver ever touching data it doesn't own?
- Should `invocation-external` be a genuinely new `Service`/`Credential`-adjacent type, or expressible as
  a `Service` with `start=None` allowed (relaxing today's "start required" constraint) plus `attach`
  always assumed? The former is cleaner conceptually; the latter reuses more existing plumbing.
- Does promoting RO to store-backed sharing (step 3) have any consumer relying on today's per-worker
  semantics as a *feature* (e.g. worker-local state a backend's `instantiate` mutates that shouldn't be
  shared)? Worth an audit of `uc`'s Databricks `ro_target`/`instantiate` before flipping this.
- Is RO sharing ever *correctly* scoped narrower than "drop `config_variant` from the projection"? E.g. if
  a `config_variant` changes what data an RO source actually contains (not just how it's fetched), sharing
  across variants would be a real bug, not just redundant work — this needs a per-case answer, not a
  blanket rule baked into the projection.
- If RW tokens become a canonical encoding of the full key rather than today's hand-built
  `<date>_<mnemonic>_<hash>`/`cell_schema_name` strings, does any consumer's naming convention depend on
  the *current* format — length limits, allowed characters in a schema/catalog name, a regex some tooling
  parses? A key-encoding change here is a real migration, not purely additive, unlike most of the steps
  above.
