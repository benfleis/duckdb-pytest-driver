# Architecture (for integrators)

The model behind the **[README's integration guide](../README.md#integrating-a-backend-worked-example--iceberg)**
— what the framework gives you and how the pieces fit. Written for someone wiring a backend (UC,
Iceberg, …) onto the driver, not for someone hacking the driver itself (that's
**[INTERNALS.md](INTERNALS.md)**).

## The shape

`duckdb-pytest-driver` is a **pytest front-end over the DuckDB `unittest` (Catch2) binary**. The
central artifact is the **`.test` (SQLLogic) file**; Python is thin glue, never the test itself.

Three test lanes:
- **`.test` alone** — collected and run through the binary. Zero Python. (Always on — the plugin's own
  collect hook.)
- **`.py` driver + same-stem `.test`** — the `.py` is collected; it provisions/decorates, calls
  `run_paired(request, env=…)` to run the body through the binary, and may add its own assertions.
- **`.py` Python-native** — an ordinary pytest test (own assertions, no SQLLogic). Enabled by
  `python_files = test_*.py` in the repo's `pytest.ini` (off by default to shield duckdb *core*'s many
  non-test `test_*.py` scripts; a `ducktest configure --test-kinds python` flag is the planned way to
  set it — see PLAN.md). All suite / credential / `@requires` machinery applies unchanged — it keys on
  the pytest *item*, not on the file being a `.test`.

(`.sql`, `.test_slow/_coverage`, and the C++ `TEST_CASE` lane are still planned — see PLAN.md.)

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
- The selection decision is derived from the CLI args and is identical on the controller and workers,
  so "what runs" and "what gets provisioned" always agree.

## The paradigm shift (read this once)

The old DuckDB `unittest`/`require-env` world **silently skipped** a test whose prerequisite was
missing. This framework inverts that:

> A test that is **selected but cannot be provisioned FAILS — loud, red, counted.** Only *deselected*
> tests (a suite not in this run) are absent.

Silent skips let "green" mean "didn't actually run." Making selection deliberate + provisioning
automatic + unprovisionable-fails is what makes automatic provisioning trustworthy. Expect failures
where you used to see skips.

## Two resource classes

Resources ride on a suite and come in two shapes, distinguished by *when* they're provisioned:

| | **credential** (class-1) | **service** (class-2) |
|---|---|---|
| when | **eager** — once, up front, on the controller | **lazy** — first worker to need it |
| why eager/lazy | an `op`/biometric prompt must land at invocation | expensive; only boot if a test actually pulls it |
| shared how | fetched once, broadcast to workers | one instance, shared across workers |
| you write | `credential(fetch, validate, error, adopt, available)` | `service(start, stop, fixture)` + a fixture |

Both are carried between the controller and workers by **the store** (below).

### Credentials

Declared with `credential(key, fetch=, validate=, error=, adopt=, available=, late_fetch=True)`. The
resolution a worker applies before running a selected credentialed test:

1. **up-front store hit** — if the suite was *predictably* selected (`-m`, path), the controller already
   fetched (`fetch` is env-first, prompting only for gaps), validated, and published it; workers read it.
   `adopt="env"` also merges it into `os.environ` so `${VAR}` in the body and any SDK see it.
2. **`available()`** — a NON-interactive env check, so creds already in your env (a preset, or the `-k`
   case up-front couldn't predict) pass with no prompt.
3. **late fetch** (default on) — otherwise fetch now, **single-flighted through the store** so at most
   ONE interactive prompt happens across all workers; on failure it's poison-pilled (all waiters fail
   fast). `late_fetch=False` for strict "never prompt mid-run" CI.
4. else **fail loud** with your `error()` message.

The net: predictable selections prompt once at invocation; `-k` and preset-env still work; a genuinely
missing credential fails clearly instead of skipping.

### Services

Declared with `service(key, start=, stop=, fixture=)`. Its session fixture calls
`provision_service(config, SVC)`, which routes `start` through the store's single-flight: the **first**
worker to pull the fixture boots the service and publishes its block; the rest **block, then reuse** it.
The controller **stops it once** at session end. If `start` fails, every waiter fails fast (poison).

A service is **demand-driven**: pulling its fixture is the signal it's needed (true even under `-k`), so
there's no reachability gate — an unselected suite simply never pulls the fixture.

## The store

A tiny process-shared state carrier: a `multiprocessing` manager the controller starts **pre-fork** and
workers connect to over a local socket (address passed in the env). You rarely touch it directly, but
its model explains the failure behavior above:

- Values are whole **JSON blocks** (a credential `{TOKEN,…}`, a service `{uri,…}`); every read is a
  private copy.
- Each key is a **write-once/read-many state machine**: `absent → PENDING → SET | FAILED`. The first
  caller claims PENDING and provisions; others block until SET (read it) or **FAILED** — the **poison
  pill**: they fail fast, no retry storm.

That's why a failed credential/service fetch fails *all* selected tests cleanly rather than each worker
re-attempting a doomed `op`/boot.

## xdist model (why "up front" matters)

Under `-n`, pytest-xdist runs a **controller** + N **worker** subprocesses; the controller doesn't
collect tests — workers do, after they're spawned. Consequences you can feel:

- **Credentials are fetched on the controller, pre-fork**, so an interactive prompt lands once at
  invocation — not N times, not minutes into a run. Workers inherit them via env + the store.
- **`-k` isn't decidable before workers collect**, so a `-k`-selected credentialed test can't be
  predicted up front — that's exactly why the worker-side backstop (`available()` → late-fetch → fail)
  exists.
- **Session scope = per-worker**, not per-invocation — which is why services coordinate through the
  store (a shared singleton) rather than a plain session fixture.

## Provisioning per-test tables (`@requires`)

Beyond suite-level credentials/services, individual tests declare the **tables** they need with
`@requires(source, access, properties, name)` (stackable) or `@requires_matrix(...)` (fan a body over
a property axis, one item per cell):

- `source` is a `Fixture("name")` (a portable SQL definition + seed, instantiated via the duckdb CLI)
  or a premade FQN string.
- `access="rw"` gets an **isolated per-test schema** (no collisions across tests/workers);
  `"ro"` references a shared source directly.
- `properties` is an **open, backend-interpreted** dict (e.g. `{"storage": "managed"}`) — the driver
  never reads it; your **provisioner** does.

The `resources` fixture provisions those specs for a test (via the suite's `provisioner`), yields the
provisioner's **bindings as `resources.env`**, and tears them down after. A driver just does
`run_paired(request, env=resources.env)` and the body's `${…}` substitute to the provisioned table.
`--repl` runs the same provisioner interactively for one selected test.

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

### Fixtures — DuckDB as the middleman

A `Fixture("name")` `source` is a **lazy named ref**, not a path: a pure value that does no I/O. The
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
**library** via `domain=`, `Fixture.parquet`/`.gen`, `Clone(...)`, and archive/directory fixtures that
unpack to a TEMP_DIR instead of a table) are on the roadmap — see PLAN.md.

## Your integration surface, in one list

- `register_suite(...)` × N in `test/conftest.py` — the only required wiring.
- `credential(...)` + its 4 callables — for a live-creds suite.
- `service(...)` + a session fixture calling `provision_service(...)` — for a container/service suite.
- a **Provisioner** (`register_provisioner`) that turns `@requires` into tables + `resources.env`.
- your `.test` bodies and `.py` drivers.

Everything else — selection, banner, credential lifecycle, container sharing, xdist coordination, the
store — is the driver's.
