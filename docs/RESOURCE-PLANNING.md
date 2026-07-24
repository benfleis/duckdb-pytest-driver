# Resource Planning: a unified provisioning model

> **Status:** proposal, validated against real code, ready to implement. Supersedes the ad hoc
> per-mechanism approach to resource sharing; does not change any test-facing interface.

**Cross-references.** Builds on `ARCHITECTURE.md` (the model as built), `SERVICES.md` (service/credential
mechanics — extended here, not replaced), and `PLAN.md` (roadmap; this folds in and supersedes the
*Multi-service dependencies* entry). Prompted by `to_init_sql` (`SERVICES.md` § *`--repl` init SQL*,
shipped 2026-07-23) turning out to be the third independently-invented "gather + dedup" mechanism.
Validated by reading real code in `az` (azurite service, planned `azure_spn` credential), `uc`
(`DatabricksProvisioner`, `OssProvisioner`), and `ice` (`IcebergProvisioner`) — see § *Validation*.

## 1. What problems are we solving?

The driver has solved "a resource is shared by more than one test — provision it once, don't redo the
work" three separate times, three separate ways:

1. **Services/credentials** (`suites.py`): shared once per invocation via the store's cross-process
   single-flight. Correct — two workers racing `docker run --name X` genuinely conflicts.
2. **RO tables** (`Provisioner._shared_ro`, `provision.py:100-114`): shared once per *worker process*, via
   a plain in-memory `set()`. Weaker than (1) for no principled reason — RO instantiation is typically an
   idempotent `CREATE ... IF NOT EXISTS`, safe to coordinate the same way; this code just predates the
   store's generalization.
3. **`--repl` init SQL** (`plugin.py`, `_repl_resource_init_sql`): a third, structurally near-identical
   "gather across reachable suites, dedup by key" loop, written because (1) and (2) didn't compose into
   something reusable.

Underneath that, five concrete problems, each with a concrete forcing case:

- **P1 — sharing scope is implicit in *which system* implemented a resource**, not a declared property.
  No single place says "this is invocation-global" the way none says "this is per-test."
- **P2 — the RO dedup key isn't matrix-cell-aware, and nothing enforces that it should be.**
  `ro_target(spec, state)`'s return value **is** the dedup key (`provision.py:108-114`); `uc`'s only real
  implementation never folds cell identity into it, because its RO sources happen to be static FQNs — an
  accident of what's been tried, not evidence it's safe in general. Azure's 3-way backend matrix
  (`{azure-az, azure-abfss, azurite-az}` sharing one test body) is exactly the case where a
  non-cell-aware key silently over- or under-shares.
- **P3 — no resource shape exists for "externally owned, no managed lifecycle at all."**
  `service()` requires `start`; a pre-provisioned real Azure/S3 account has no boot and no teardown, it
  just permanently exists. Azure's two real-cloud matrix cells and httpfs's public read-only server both
  need this and can't express it today.
- **P4 — batching and resource-sharing compute "what can this item share" independently**, for the same
  underlying question. `collect.py`'s `_batch_key` (`(binary, working_dir)`) and the resource-sharing key
  above are unrelated code answering the same shape of question — concretely risking two items that must
  stay isolated (env-sensitive tests sharing a worker; two matrix cells of a future fanned `.test` file)
  landing in the same batch purely because an unrelated pair of fields happened to match.
- **P5 — Iceberg's `rest`→`minio` ordering** (`Service.depends_on` exists, unresolved) is "provision
  before first use, teardown after last use," solved once, for two services, not generalized.

**Concrete consumers this is scoped against** (not hypotheticals): `--repl`/azurite (shipped); RO/RW
sharing generally; azure's 3-way backend matrix; httpfs's analogous matrix (not yet built); UC
Databricks/OSS (already mostly conformant); Iceberg (`depends_on`, plus a real stress-test of "shared
resource" via its per-worker embedded Spark connection).

## 2. What's the approach?

**One canonical key per test item, computed once**, in a new "decorate" step inserted between *scan* and
*plan* in the existing collect-first pipeline (`ARCHITECTURE.md` § *xdist model*). Every downstream
decision — batching, resource sharing, token generation, provisioning order — becomes a projection or
policy function *over that one key*, not an independently invented mechanism.

```
key(item) = (build, run_setting, backend, access, cell)
```

- `build`, `run_setting` — constant for a whole invocation (or a whole worker, if `run_setting` is ever
  partitioned): which binary, which whole-run config sweep (e.g. httpfs's `{curl, httplib,
  connection_caching, dynamic}`). Never varies test-by-test.
- `backend`, `access`, `cell` — vary per test: which account/service this test talks to, whether it's
  `rw` (private) or `ro` (shareable), which matrix cell if any.

**Every resource also has two independent, composable properties** — not five flat mutually-exclusive
scopes (an earlier draft of this doc had that wrong):

1. **Where does the account/connection come from?** `invocation-managed` (we boot it, tear it down) /
   `invocation-attached` (already running, we just connect) / `invocation-external` (permanently exists,
   outside our control — the new one).
2. **Does a specific resource riding on that connection get shared, or does this test get its own
   private copy?** `per-invocation-shared` (ro, promoted from today's per-worker `_shared_ro`) /
   `per-test-isolated` (rw, via a token).

These compose, and often nest: Databricks' catalog is `invocation-external`; the schema created inside it
is `per-test-isolated`; the table inside *that* schema is too, but doesn't need independent teardown
tracking — dropping the schema (`CASCADE`) takes it with it. Azure's real-cloud account is
`invocation-external`; its read-only `DATA_DIR` is `per-invocation-shared`; a write test's `TEMP_DIR`
prefix under the same account is `per-test-isolated`. "Where the connection came from" and "how a given
piece of data on it is shared" are independent questions at every layer.

**Explicitly not solved here:** fanning one bare `.test` file into N matrix-cell pytest items is still
blocked by Catch2's one-file-one-registered-test model (a C++ test-harness constraint, not a driver
choice) — this proposal makes *what gets provisioned per cell* uniform and correct, not *how a `.test`
file becomes N items*, which is separate follow-on design work. Whole-invocation config sweeps (httpfs's
`{curl, httplib, ...}`) stay as repeated `pytest --unittest-args='--test-config ...'` invocations — that
mechanism already exists and fits what this axis actually is (a DBConfig setting, not a resource).

## 3. What's the design?

### The key, as data

```yaml
- test: test/azurite/azure.test
  build: az/build/debug/test/unittest
  run_setting: null
  backend: azurite-az
  substrate: invocation-managed          # AZURITE_SERVICE, booted by this run
  access: ro
  token: null
  extras: {AZURE_STORAGE_CONNECTION_STRING: "...", AZ_DATA_DIR: testing-private}

- test: test/azurite/azure.test           # hypothetical matrix cell, same file
  build: az/build/debug/test/unittest
  run_setting: null
  backend: azure-az
  substrate: invocation-external          # permanently exists, never booted/torn down
  access: ro
  token: null
  extras: {AZ_STORAGE_ACCOUNT: duckdblabstestdatablob, AZ_DATA_DIR: duckdblabs-data/common/azure_data}

- test: test/azure/azure_writes.test
  build: az/build/release/test/unittest
  run_setting: null
  backend: azure-az
  substrate: invocation-external
  access: rw
  token: "20260724_azwrite_a1b2c3"         # the token IS the write path's unique segment
  extras: {AZ_TEMP_DIR: duckdblabs-write-testing/extension/azure/20260724_azwrite_a1b2c3}

- test: uc read test
  build: uc/build/release/test/unittest
  run_setting: null
  backend: uc-databricks
  substrate: invocation-external           # the CATALOG; its schema is created per-test, not this
  access: ro
  token: null
  extras: {CATALOG: my_write_catalog, SCHEMA: main, TABLE: simple_table}
```

`build`/`run_setting` repeat for every job in a run; `backend`/`substrate`/`access`/`token` are what
actually decide sharing; `extras` is delivered, never consulted for sharing decisions.

### Four small planner functions, over that data

```python
def batch_key(job):
    """Jobs sharing this may run in the same unittest subprocess call."""
    return (job.build, job.run_setting)

def coordination_key(job):
    """None => nothing to wait on (rw: always private; invocation-external: always just there).
    Otherwise: the store key jobs with the SAME value single-flight around."""
    if job.access == "rw" or job.substrate == "invocation-external":
        return None
    return (job.backend, job.access)

def token(job):
    """Only rw jobs get one -- unique per (build, backend, test, nodeid). Already exists today as
    _provision_token (plugin.py:1459) -- this formalizes its guarantee, doesn't replace it."""
    return stable_hash(job.build, job.backend, job.test, job.nodeid) if job.access == "rw" else None

def env_for(job):
    """The one genuinely backend-specific function -- every consumer supplies its own -- but always
    the same shape: (job, its token) in, a flat env dict out."""
    return BACKEND_ENV_RESOLVERS[job.backend](job, token(job))
```

`batch_key` deliberately excludes `backend`/`cell` — not because they don't matter, but because two cells
can never share one subprocess call anyway (the Catch2 wall), so keeping the projection narrow is what
*prevents* accidental cross-cell contamination once fan-out exists, rather than requiring new
special-casing later.

### Walked through scan → decorate → plan → execute

Take the azurite-az RO job and the azure-az RW job above: **scan** collects both, same as today.
**decorate** computes their keys — pure, no side effects. **plan**: their `batch_key`s match (same `az`
binary, no `run_setting`), so they're *eligible* to share a subprocess call (ordinary xdist batching
decides whether they actually do); their `coordination_key`s differ (`("azurite-az","ro")` vs. `None`) —
the RO job gets single-flighted through the store, the RW job never coordinates with anyone, it just gets
its own token. **execute**: whichever happens, the two jobs never contend, because coordination and
token assignment were already resolved before either one ran.

## 4. Validation against real code

- **UC's `DatabricksProvisioner`** (`engine.py:259-320`): promoting RO to store-backed sharing is safe.
  Its worker-persistent state (`_refs`, `_cell_for_default`) resets at the top of every `provision()`
  call — unrelated to `_shared_ro`'s cross-call persistence. `ro_target` populates `state.tables`/`_refs`
  *unconditionally*, before the shared/skip check — a worker that loses the single-flight race still
  gets correct env output; only the redundant `execute()` DDL would additionally be skipped.
- **UC's `identity.py`** already exports the generic `CATALOG`/`SCHEMA`/`TABLE` vocabulary this proposal
  wants as a driver-level default — it just isn't promoted out of UC yet, so Iceberg (which has the same
  shape of need) can't reuse it without copying it.
- **Iceberg's `IcebergProvisioner`** (`provisioner.py`): its RO tables generate through an embedded,
  deliberately-per-worker PySpark connection ("each xdist worker gets its own embedded session"). Looks
  like proof per-worker sharing is sometimes inherent — it isn't: the generated data lands in a warehouse
  path that's the same for every worker; only *who executes the generate() call* is worker-bound. Store
  coordination ("can this be skipped, it's done") and execution ownership ("who does it when it's needed")
  are separable — the first worker to win the race runs its own connection, everyone else just reads what
  got written.
- **Iceberg's `minio`/`rest` services** are still a raw `docker-compose.yml`, not declared as driver
  `Service`s — the `depends_on` ordering case (P5) is still plan-only there, not checked against real code.
- **`TEMP_DIR` teardown** is already substrate-agnostic and needs no new design: `--temp-dir-destroy
  {never,on-success,always}` (`plugin.py:189-197`, default `on-success`) plus a remote sweep that's
  keep-on-failure by construction already apply the same regardless of what's under the path.
- **Token generation is already driver-core**: `_provision_token` (`plugin.py:1459`) is the one function
  every `.provision()` call site uses today (both the ordinary `resources` fixture path and `--repl`),
  producing a `[0-9a-zA-Z_]`-safe string; `uc`'s `cell_schema_name` already incorporates it as a substring
  rather than reinventing it. What's missing is turning that observed convention into an enforced,
  documented contract, not building anything new.
- **RO sharing across `run_setting` values**: asserted as a hard rule rather than a per-case judgment —
  `run_setting` is *defined* to never affect what data an RO source contains. If a real case seems to
  need it to, that's the test modeled wrong (should be a different `backend`), not a reason to widen the
  sharing key.

## 5. Phased implementation

No big-bang rewrite; each phase is independently shippable and (except phase 4) behavior-preserving.

| # | phase | repo(s) | risk |
|---|---|---|---|
| 1 | Canonical per-item key as a real data structure ("decorate"), initially carrying just `(build, run_setting)` — same as today's `_batch_key` | driver | none — pure refactor |
| 2 | Dedupe the reachable-suite gather loop (`provision_reachable` + `_repl_resource_init_sql` → one shared helper) | driver | none |
| 3 | Promote `_shared_ro` to store-backed single-flight | driver | low — behavior-preserving per § 4 |
| 4 | Extend the key with `backend`/`access`/`cell`; make RW-token uniqueness and RO-shared-identity explicit, named guarantees over the key (generators unchanged, see § 4) | driver | **touches the `Provisioner` contract every consumer implements** |
| 5 | `service()`'s `start=None` allowed; `provision_service` routes to `attach()` unconditionally when there's no `start` — this *is* `invocation-external`, no new type | driver | low, additive |
| 6 | Resolve `depends_on` (topological order + reverse teardown) across the unified resource-node set | driver | subsumes Iceberg's P5 |
| 7 | Promote `CATALOG`/`SCHEMA`/`TABLE` (already in `uc/identity.py`) to a driver-owned default vocabulary | driver, then uc/ice adopt | low |
| 8 | `az`: collapse `AZ_DATA_DIR`/`AZ_TEMP_DIR`/`ABFSS_*` to plain `DATA_DIR`/`TEMP_DIR` now that the `{proto}` `foreach` loop moves out into the matrix mechanism instead of living in the test body | az | touches every `.test` body |
| 9 | *(separate track, not gated on 1–8)* Suite-level matrix (`register_suite(matrix=...)`): `.py` fan-out via `pytest_generate_tests` (reuses `@requires_matrix`'s `matrix_cell` indirect plumbing), `.test` fan-out via a post-collection list-splice in `pytest_collection_modifyitems` (`collect()` stays a plain single yield); `_batch_key` gains `cell` | driver | **landed** (`suites.py`/`plugin.py`/`collect.py`; `tests/test_suite_matrix.py`) |

## 6. Remaining open questions

- Phase 9 answered its own mechanical question: collector-native (a list-splice after `collect()`
  runs), not generated files — see `plugin.py`'s `_expand_test_matrix`.
- Suite membership itself is still coarse (path-subtree + a per-item marker escape hatch, no
  include/exclude/pattern list) — the same underlying gap as "how do we declare a weekly smoke-test
  subset out of 1000+ tests." Phase 9's composition rule (a test's own `@requires_matrix` wins over
  its suite's `matrix=`, with a verbose-mode note) is contingent on that coarseness; a richer
  suite-membership mechanism, if ever built, would likely flip this to fail-loud-on-conflict instead.
- Is declaring `run_setting` (httpfs's sweep) as a real key component ever worth building, or does it stay
  a documented-but-unimplemented capability indefinitely? Not currently justified by need.
- For `invocation-external` resources reached over `TEMP_DIR`, does the existing remote-reap sweep need a
  narrower prefix convention so the driver never touches data outside what it wrote? (Low risk, per § 4,
  but not yet stress-tested against a real `invocation-external` write path.)
