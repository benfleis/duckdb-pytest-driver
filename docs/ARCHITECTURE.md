# Architecture (for integrators)

The model behind the **[README's integration guide](../README.md#integrating-a-backend-worked-example--iceberg)**
— what the framework gives you and how the pieces fit. Written for someone wiring a backend (UC,
Iceberg, …) onto the driver, not for someone hacking the driver itself (that's
**[INTERNALS.md](INTERNALS.md)**).

## The shape

`duckdb-pytest-driver` is a **pytest front-end over the DuckDB `unittest` (Catch2) binary**. The
central artifact is the **`.test` (SQLLogic) file**; Python is thin glue, never the test itself.

Three test lanes:
- **A bare body alone** (`.test`, or `.test_slow`/`.test_coverage` with `--slow`) — collected and run
  through the binary. Zero Python. `.test` is always on (the plugin's own collect hook); the slow
  suffixes are marked `slow` and run one test per binary invocation.
- **`.py` driver + same-stem body** (`.test`, `.test_slow`, or `.test_coverage`) — the `.py` is
  collected and the body is suppressed; the driver provisions/decorates, calls
  `run_paired(request, env=…)` to run the body through the binary, and may add its own assertions.
  Paired to a slow/coverage body, the driver itself needs `--slow` too, same as a bare one would —
  there is no separate "slow driver" concept, just a driver whose body happens to be slow.
- **`.py` Python-native** — an ordinary pytest test (own assertions, no SQLLogic). Enabled by
  `python_files = test_*.py` in the repo's `pytest.ini` (off by default to shield duckdb *core*'s many
  non-test `test_*.py` scripts; a `ducktest configure --test-kinds python` flag is the planned way to
  set it — see PLAN.md). All suite / credential / `@requires` machinery applies unchanged — it keys on
  the pytest *item*, not on the file being a `.test`.

(`.sql` and the C++ `TEST_CASE` lane are still planned — see PLAN.md.)

`ducktest configure` writes the `pytest.ini` a plugin can't inject (`testpaths`, `-n auto`,
`--import-mode=importlib`). After that, bare `pytest` runs the suite. The plugin auto-registers via a
`pytest11` entry point — no `pytest_plugins` line.

## Suites and selection

A **suite** is a named group of tests (by directory `path`) with a **default-run policy** and a set
of **resources**. You declare suites with `register_suite(...)` in `test/conftest.py`; everything else in
this doc hangs off that.

- **Auto-marker.** The driver stamps each suite's `marker` on every member under its `path` — including
  `.test` bodies that can't carry a Python `@mark`. So `-m rest` / `-m 'not rest'` work with no manual
  marking.
- **Default selection.** A **bare `pytest`** (no path, `-k`, or `-m`) runs only `default=True` suites —
  the fast "smoke" set — and **deselects the rest, announcing it with a banner**. Any explicit
  selection (`-m`, `-k`, a path) is honored verbatim and turns the default scan off. Note "smoke" is
  *just this default set* — there is **no built-in `smoke` marker** (auto-markers are the *suite* names);
  the standard vocabulary (`smoke/local/cloud/slow/all`) is a convention, and making it first-class is on
  the roadmap (PLAN.md § *Named test sets*).
- The selection decision is derived from collected items and is identical on the controller and workers,
  so "what runs" and "what gets provisioned" always agree.
- **`provisioner=`** registers a path-scoped provisioner for `@requires` tables (§ *Provisioning per-test
  tables*). **`auto_init_sql=True`** feeds the suite's credential/service init SQL into bare `.test`
  bodies and `--repl` (`docs/MATRIX.md` § *Init SQL and test configs*).
- **Overlapping membership.** An item under two suites' paths (or markers) belongs to both: markers from
  both are stamped, and services/credentials union (deduped by key). Anything that needs exactly one
  owner picks the first suite in registration order — e.g. if both suites declare a `matrix=`, only the
  first one's cells apply to the shared item; the second's matrix never fires for it. There is no
  fan-out and no conflict error; declaring two suites over the same files is legal.

## The paradigm shift (read this once)

The old DuckDB `unittest`/`require-env` world **silently skipped** a test whose prerequisite was
missing. This framework inverts that:

> A test that is **selected but cannot be provisioned FAILS — loud, red, counted.** Only *deselected*
> tests (a suite not in this run) are absent.

Silent skips let "green" mean "didn't actually run." Making selection deliberate + provisioning
automatic + unprovisionable-fails is what makes automatic provisioning trustworthy. Expect failures
where you used to see skips.

This is not fully enforced for suite services yet. Collect-first service provisioning is best-effort:
its error is suppressed until a fixture calls `provision_service`. `run_paired` now does this itself
for its item's suite services, so a paired driver re-surfaces the original error even with no
service-consuming fixture. A true bare `.test` (no `.py` at all) still has no such pull point, so it
may only fail later because the service environment is missing.

## Suite resources

Suites declare credentials and services. Collect-first derives the reachable suites from pytest's final
selection, including `-k`, then provisions every credential those suites need and every service the
selection's resolved matrix cells actually need — a cell's `services=` key narrows the suite's declared
set (`docs/MATRIX.md`); absent, a cell needs all of them. Both resource types use the shared store for
single-flight provisioning and controller/worker delivery.

### Credentials

Declared with `credential(key, fetch=, validate=, error=, adopt=, available=, to_init_sql=)`.

For each credential needed by the final selection:

1. `available()` may confirm that ambient credentials are already usable, avoiding a fetch.
2. Otherwise `fetch(config)` runs through the store's single-flight path.
3. `validate(value)` rejects an unusable value. The session aborts with `error()` or a default message.
4. `adopt="env"` makes the fetched value resolvable, on demand, for items belonging to the
   credential's own suite — SQLLogic substitution there, not a global `os.environ` merge.

There is no per-test credential resolution. A selected test's credential is settled before execution.

### Services

Declared with `service(key, start=, stop=, fixture=, attach=, alive=, depends_on=, to_env=, populate=,
to_init_sql=)`. `provision_service(config, service)` has two lifecycle stances with the same block shape:

- **Managed** — `start` single-flights through the store: exactly one boot for the whole run. Workers
  reuse the published block. `populate` single-flights separately, scoped per DECLARING suite rather
  than per key — a key shared across suites via `use_service(..., populate=...)` runs each suite's
  own `populate` once, cumulatively, against the one booted resource. The controller calls `stop`
  once at session end.
- **Existing (external)** — the service is already running (started by you, or living on the host while
  pytest runs in a container); declare it via `--existing-service KEY[=URL|={json}]` (or an env var) and
  the run **attaches** instead of booting: `attach(overrides, config)` builds the block, `alive(block)`
  probes it once (a declared-but-dead service **fails loud**, the paradigm shift applied to attach) — no
  store, no teardown. `docs/SERVICES.md` is the deep-dive (the block/derive contract, the attach entry
  grammar, and the `ducktest provision-service`/`teardown-service` out-of-session commands that pair with
  it — bring a service up, hand you the exact attach line, leave it running for a later in-container run).

A service with no `start` is always external and attaches with default overrides. `depends_on` orders
startup and reverse-order teardown. `populate(block, config)` must be idempotent regardless of path —
attached services are populated on every attach, and a managed service's populate can run once per
suite that binds it, not just once globally. `to_env(block)` resolves on demand, scoped to whichever
item's own suite
declares the service, and is registered for that item's SQLLogic substitution — including for bare
`.test` subprocesses. It is not a global `os.environ` merge: a different suite's item never sees it.
A fixture may call `provision_service` to consume the same block; it does not control whether the
service is provisioned.

A service/credential can also carry **`to_init_sql(block|value, *, redact=False) -> str`** — SQL fed
into a `--repl` session's `-init` file (e.g. azurite's default connection secret), so a bare-`.test`,
provisioner-less suite's `--repl` isn't dropped with the connection env set but nothing typed for you.
See **docs/SERVICES.md** § *`--repl` init SQL*.

## The store

A tiny process-shared state carrier: a `multiprocessing` manager the controller starts **pre-fork** and
workers connect to over a local socket (address passed in the env). You rarely touch it directly, but
its model explains the failure behavior above:

- Values are whole **JSON blocks** (a credential `{TOKEN,…}`, a service `{uri,…}`); every read is a
  private copy.
- Each key is a **write-once/read-many state machine**: `absent → PENDING → SET | FAILED`. The first
  caller claims PENDING and provisions; others block until SET (read it) or **FAILED** — the **poison
  pill**: they fail fast, no retry storm.

Credentials and fixture-consumed managed services reuse a stored failure instead of retrying a doomed
fetch or boot. Bare `.test` service failures are subject to the limitation above.

## xdist model

Under `-n`, pytest-xdist runs a controller and N workers. Ducktest performs an explicit controller-side
collection before execution, builds a plan from the selected items, and provisions the plan's suite
resources. Workers collect normally and consume the shared store. They also run the same idempotent
provisioning pass for their selected suites, so environment adoption is process-local while fetches and
managed starts remain single-flight. Pytest session scope is per worker; the store provides
invocation-wide coordination.

## Provisioning per-test tables (`@requires`)

Beyond suite-level credentials/services, individual tests declare the **tables** they need with
`@requires(source, access, properties, name)` (stackable) or `@requires_matrix(...)` (fan a body over
a property axis, one item per cell):

- `source` is one of: a `TableSpec("name")` (a portable SQL definition + seed, instantiated via the
  duckdb CLI); a premade FQN string; or a **backend-defined lazy ref** — any other object, opaque to the
  framework and interpreted entirely by your provisioner's `instantiate()`, the same open/backend-owned
  spirit as `properties` (e.g. Iceberg's `IcebergDef("default/…")`, a ref into its own generator
  registry). A lazy-ref source **requires an explicit `name=`** (there's no generic way to derive a bare
  name from an arbitrary object; `requires()` fails loud without it).
- `access="rw"` gets an **isolated per-test schema** (no collisions across tests/workers);
  `"ro"` references a shared source directly.
- `properties` is an **open, backend-interpreted** dict (e.g. `{"storage": "managed"}`) — the driver
  never reads it; your **provisioner** does.

The `resources` fixture provisions those specs for a test (via the suite's path-scoped `provisioner`), yields the
provisioner's **bindings as `resources.env`**, and tears them down after. A driver just does
`run_paired(request, env=resources.env)` and the body's `${…}` substitute to the provisioned table.
`--repl` runs the same provisioner interactively for one selected test. Declaring
`register_suite(..., path="test/backend", provisioner=...)` registers that provisioner for the suite path;
`register_provisioner(...)` remains available for a narrower subtree override.

**Table naming/addressing contract — a recommended convention, not (yet) a driver feature.** (Called the
"identity contract" elsewhere in older notes — avoid that name going forward: it reads as
auth/credentials, but this is entirely about *addressing provisioned tables*, disjoint from the
credential system above. Rename the module/docs together when this promotes — see PLAN.md § *Fixtures*.)
The driver's `resources`
fixture is **opaque about `resources.env`'s shape**: it hands back whatever your `provision()` returns
(the driver only requires a `.env` dict for `run_paired(env=…)`). *Which* keys go in it is your
provisioner's choice. The convention worth adopting is a small uniform vocabulary
(`CATALOG`/`SCHEMA`/`TABLE`, plus `{KEY}` FQN aliases for multi-table tests) so a body reads the same
across backends. **Today that vocabulary lives in UC** (`uc/test/py/uc/identity.py`, `TableRef` +
`build_env`), **not** in `ducktest` — an integrator implements it in their own provisioner,
or reuses UC's `identity.py`. Promoting it into a driver `Provisioner` base is on the roadmap
(PLAN.md § *Fixtures*).

### Source refs (what `@requires(source=…)` accepts)

`source` is a **lazy reference to where a table comes from**. The three forms above (a `TableSpec`, an FQN
string, a backend-defined object) share one contract, and everything the driver guarantees hangs off it:

- **It is a pure value.** Constructing a source ref does no I/O: no file read, no catalog call, no
  generation. It just holds a name or handle. Collection evaluates every `@requires` decorator to attach
  its marker, so a ref that did work at construction would make every collected test pay for it even when
  deselected or skipped. Resolution happens only when a *running* test provisions the requirement.
- **The driver never interprets it.** A source is opaque to the framework, the same stance as
  `properties`. The one thing the driver needs is a bare table name for the provisioned schema; a
  `TableSpec` and an FQN string carry that themselves, so any other kind of source **must pass `name=`**
  (`requires()` fails loud otherwise).
- **The provisioner's `instantiate(spec, target, dry_run, bindings)` turns it into a real table.** This is
  the extension point: your provisioner reads `spec.source`, does whatever that kind of source means (run
  the SQL, call a generator, reference a premade table), and lands a table at `target`. It **must honor
  `dry_run`** (plan only, no DDL). The body then addresses that table through `resources.env`.

That contract is all that's shared. The *kinds* of source differ in more than syntax, and the axis that
separates them is **not** "which query language" (a `TableSpec` and Iceberg's `IcebergDef` are both SQL
files). It is portability and ownership:

| | `TableSpec` (built-in) | a backend-native ref (e.g. Iceberg's `IcebergDef`) |
|---|---|---|
| owns the definition | the driver's portable fixture library | the extension's own registry |
| portability | one fixture drives any backend (duckdb canonicalizes; the instantiator maps types + applies `properties`) | backend-locked by nature (its ops are that backend's) |
| logical vs physical | separated: the fixture says *what*; `properties` + the instantiator decide *how it's stored* | fused: the def *is* the physical recipe |
| shape | a schema + seed, with an independent `.Seed()` override | often a procedure (create → insert → evolve → …), not a static schema + seed |

So the real split is **a portable, driver-owned fixture vs a backend-native recipe.** `TableSpec` is the
built-in member (below); a backend adds its own ref type when its tables can't be expressed as a portable
fixture, as Iceberg's `IcebergDef` points at an entry in the extension's Spark generator registry. A
shared named base (`TableSource` or similar) is deferred until a second backend needs one; for now the
members stay separately named and this contract is what unifies them.

### Fixtures — DuckDB as the middleman

A `TableSpec("name")` `source` is a **lazy named ref**, not a path: a pure value that does no I/O. The
framework resolves the name against a search path only when a *running* test instantiates it —
collecting or skipping reads nothing. The fixture itself is a tiny SQL file — schema + seed, and
**nothing about physical storage**:

```sql
-- fixture: id_name
-- keys: [id]
CREATE TABLE id_name (id INTEGER, name VARCHAR);
INSERT INTO id_name VALUES (1,'a'), (2,'b'), (3,'c');
```

The logical/physical split is the point: the fixture says *what the table is*; the backend-interpreted
`properties` (storage layout, catalog-managed props, LOCATION, …) are the **instantiator's** job. So
one fixture drives every backend — only the instantiator changes.

We never hand-parse the SQL or restate types: **DuckDB is the converter.** The fixture body runs
through the located `duckdb` CLI (no python-duckdb dependency; resolved via `find_duckdb`), and the
resolved schema (`DESCRIBE`) + seed rows (`SELECT *`) are read back as a canonical `Table` — the
hand-off to any non-duckdb backend:

```
fixture.sql ──duckdb CLI──▶ Table{columns:[(name,type,nullable)], seed_data:[…]}
             (CREATE+INSERT)         ├─ DuckDBInstantiator: target IS duckdb — nothing to translate
                                     └─ backend Instantiator: map_columns(TYPE_MAP) → apply properties
                                                              → seed rows (VALUES / parquet)
```

The last mile is a registered **`Instantiator`** (`register_instantiator(config, impl, scope=…)`,
scoped by test location like `register_provisioner`) implementing one method
`instantiate(definition, target, *, duckdb_bin) -> Table`. `DuckDBInstantiator` (the default) just runs
the body into a per-test db file; a backend instantiator canonicalizes, maps types (fail-loud on
unmapped), applies its `properties`, and seeds the rows. Future fixture kinds (a shared fixture
**library** via `domain=`, `TableSpec.parquet`/`.gen`, `Clone(...)`, and archive/directory fixtures that
unpack to a TEMP_DIR instead of a table) are on the roadmap — see PLAN.md.

## Your integration surface, in one list

- `register_suite(...)` × N in `test/conftest.py` — the only required wiring.
- `credential(...)` — for a live-credentials suite.
- `service(...)` — for a managed or attached service. Bind suite-specific `to_env`, `populate`,
  `depends_on`, or `to_init_sql` behavior with `use_service(...)`. A fixture may call
  `provision_service(...)` to consume its block. See § *Services*.
- a **Provisioner** (`register_suite(..., path=..., provisioner=...)`) that turns `@requires` into tables +
  `resources.env`; use `register_provisioner` only for a narrower subtree override.
- your `.test` bodies and `.py` drivers.

Everything else — selection, banner, credential lifecycle, container sharing, xdist coordination, the
store — is the driver's.
