# Table fixtures — SQL definition + seed, with DuckDB as the middleman

Status: **pure-DuckDB tier prototyped** (`src/duckdb_pytest_driver/fixtures.py`,
runnable demo in `examples/pure-duckdb/`). Databricks/Iceberg instantiators are the
next tier — the seam is in place; the instantiators are not yet written.

## The idea

A test declares the table it needs; the framework instantiates it. The declaration
is backend-agnostic; the instantiation is per-provider.

```python
@requires(source=Fixture("simple_table"), access="rw", properties={"commit": "cmt", "storage": "managed"})
def test_reads(resources):
    t = resources.tables["simple_table"]
    assert t.seed_data == [(1,), (2,), (3,), (4,), (5,)]
```

`Fixture("simple_table")` is a **named ref, not a path** — a pure value that does no
I/O. The framework resolves the name against a search path only when a *running* test
instantiates it; collecting or skipping a test reads nothing.

The fixture is a tiny SQL file — schema + seed, and **nothing about physical storage**:

```sql
-- fixture: simple_table
-- keys: [id]
CREATE TABLE simple_table (id INTEGER);
INSERT INTO simple_table VALUES (1), (2), (3), (4), (5);
```

The logical/physical split is the whole point. The fixture says *what the table is*.
The backend-interpreted `properties` (e.g. the `commit`/`storage` 2×2 — LOCATION,
catalog-managed `TBLPROPERTIES`, …) are the **instantiator's** job. So one fixture drives
every backend; only the instantiator changes.

## Why not a bespoke format — DuckDB *is* the converter

We never hand-parse the SQL or restate column types in the header. We run the body
through the located duckdb CLI (`<build>/duckdb`, the same binary `--repl` uses — **no
python-duckdb dependency**; resolved via `find_duckdb`, i.e. `--build`/`$BUILD_DIR`/`--duckdb-bin`) and read back the resolved schema (`DESCRIBE`) and seed data
(`SELECT *`) as JSON. That canonical `Table` is the hand-off to any non-duckdb
instantiator.

```
fixture.sql ──duckdb CLI──▶ Table{ columns:[(name,type,nullable)], seed_data:[…] }
             (CREATE+INSERT)          │
                                      ├─ DuckDBInstantiator:  ran the SQL — nothing to translate
                                      └─ SparkInstantiator:   map types + emit rows (VALUES / parquet)
```

Header fields (all optional): `fixture:` (name; defaults to file stem), `table:`
(disambiguate if the body creates >1 table), `keys:` (`[a, b]`). Column **types are
not declared** — duckdb resolves them from the `CREATE`. Bodies should be DDL + seed
only (no result-returning statements).

## What `source=` accepts — self-contained fixtures vs. a live clone

The line that matters is **self-contained definition** vs. **reference to live external
state**. A `Fixture` carries or deterministically *produces* its def+data; a clone
reaches out to whatever currently lives at an FQN. The type carries that meaning:

| `source=` | Kind | Self-contained? | Role |
| --- | --- | :---: | --- |
| `Fixture("id_name")` | SQL definition + seed | ✅ | **default** — small functional tables |
| `Fixture.parquet("f.parquet")` *(future)* | parquet (schema+data in one file) | ✅ | pre-baked data |
| `Fixture.gen("tpch", sf=1)` *(future)* | generator (`CALL dbgen`/`dsdgen`) | ✅ | large / computed data |
| `Clone("cat.sch.tbl")` *(escape hatch)* | CTAS from a live catalog table | ❌ | clone an existing table |

**Every `Fixture` kind is just a different duckdb *body* that creates the table** —
`.sql` → the file text; `.parquet` → `… AS SELECT * FROM read_parquet(...)`; `.gen` →
`CALL dbgen(...)`. Canonicalization (instantiate → `DESCRIBE`/`SELECT`) is invariant
across all of them, which is why one middleman covers every kind.

CTAS is the *one* case that can't be a self-contained body (it reaches outside), so it
stays a separate marker rather than a `Fixture` kind — cramming it in would blur the
self-contained/external line. It's also largely **vestigial**: the reason to CTAS was
"large data is costly to re-insert," but the generator kind covers that
self-containedly (no premade external table). Today the legacy path is still a bare FQN
string (`source="cat.sch.tbl"`, back-compat); `Clone(...)` is the cleaner future
spelling. Only `Fixture` (with `domain`, explicit-kind constructors, and a `.Table`
vs `.Data` split) is designed further — see "Next" below.

## The instantiator seam (last mile)

Generic core (format, load, `canonicalize`) ships in the driver. The last mile is a
registered `Instantiator`, scoped by test location exactly like `register_provisioner`:

```python
# a backend conftest
from duckdb_pytest_driver import register_instantiator
def pytest_configure(config):
    register_instantiator(config, MySparkInstantiator(), scope=os.path.dirname(__file__))
```

An `Instantiator` implements one method:

```python
def instantiate(self, definition, target, *, duckdb_bin) -> Table: ...
```

`DuckDBInstantiator` (the built-in default) is trivial — the target *is* duckdb, so it runs
the definition body into a db file (the file is the per-test isolation boundary) and
returns the resulting table. A non-duckdb instantiator:

1. `canonicalize(duckdb_bin, definition)` → `Table`;
2. `map_columns(table, TYPE_MAP)` → its own DDL types (fail-loud on unmapped types);
3. apply the backend `properties` (e.g. the `commit`/`storage` 2×2 — its `TBLPROPERTIES`/`LOCATION`) around that schema;
4. seed `table.seed_data` (as `VALUES`, or via a parquet duckdb wrote).

Example provider type map (ships with the provider, not the driver):

```python
SPARK_TYPE_MAP = {"INTEGER": "INT", "BIGINT": "BIGINT", "VARCHAR": "STRING",
                  "DOUBLE": "DOUBLE", "BOOLEAN": "BOOLEAN", "DATE": "DATE",
                  "TIMESTAMP": "TIMESTAMP", "DECIMAL": "DECIMAL"}
```

For the Databricks backend this slots into the *existing* 2×2 instantiator with a one-line
change: `_build_table_sql`'s trailing `AS SELECT * FROM {source_fqn}` becomes
`AS SELECT * FROM (VALUES …)` built from the table's seed data — the props/location
machinery is untouched, and the premade-`source.*` dependency drops out.

## What's built vs. next

- **Built:** the `Fixture` named ref (lazy — no I/O at collection); the format +
  loader + duckdb canonicalizer + `Instantiator` seam + `DuckDBInstantiator` + `map_columns`
  helper; a runnable pure-duckdb demo; self-tests (incl. a deferred-load proof).
- **Next — `Fixture` design:**
  - **`domain`** — a named, registered fixture root, so fixtures become a **shared
    library across repos**. Concretely: duckdb core ships a large set of table/data
    fixtures; an external repo submodules core (and others), registers each
    (`register_fixture_root(config, path, domain="core")`), and freely references
    `Fixture("some_table", domain="core")` / `Fixture("other_table", domain="ext2")`
    instead of rebuilding its own. Default (no `domain=`): the registering conftest's
    root(s), searched in order. Prefer the `domain=` kwarg over a dotted-in-name form
    to keep it unambiguous vs. a `Clone(...)` catalog FQN.
  - **explicit-kind constructors** — `Fixture.parquet(...)`, `Fixture.gen("tpch", sf=1)`;
    bare `Fixture(name)` stays the SQL default. Each just builds a different body.
  - **seed sources / def-vs-data** — `.Seed()` today takes `None` (empty) or a literal
    list of row tuples. The form should generalize to a lazily-resolved **seed source**:
    a **generator** (`.Seed(Gen("tpch", sf=1))` — computed/large, never a literal list)
    or **another fixture's data** (`.Seed(Fixture("id_name_big").data)` — one table
    *definition*, rows borrowed from a different fixture: the `.Table`/`.Data` split,
    structurally enforced so they prove the same shape). Crucially this is the SAME
    "data producer" abstraction as `source=` (SQL / parquet / generator / clone) — one
    row source plugs into either slot, and resolves lazily (no materialization at
    decoration), exactly like a `Fixture` ref.
- **Next — instantiators / consumers:** a Spark/Databricks instantiator (type map +
  rows-as-VALUES into the 2×2); route the OSS + Databricks *run* paths through
  `resources`/`Fixture` so the driver `.py` collapses to one shape across backends.

## A different beast — archive / directory fixtures (shaping note)

Not every fixture is a table def + rows. Some are a **pre-formatted directory tree**
delivered as a zip/archive — e.g. Delta Acceptance Tests: a whole Delta table dir
(`_delta_log/`, parquet, …) that a test runs against as-is. It's self-contained (the
archive carries everything), so it's a `Fixture` *kind* — but its instantiation is
"unpack a dir," not "make a table." A special case; it must be **doable through the
same seams without being the tail that wags the dog**. What it needs:

- **Resolution** — same named-ref + `domain` root machinery; the backing is an archive,
  not a `.sql`.
- **Instantiation → a path, not a table** — unpack into a managed **TEMP_DIR** (the
  driver's managed-temp machinery — see TEMP_DIR notes), per-test isolated, torn down
  after. This is the one kind that **bypasses the DuckDB middleman**: there's no
  schema/rows to canonicalize, so `instantiate()` returns a *directory handle*, and
  `resources` must expose heterogeneous kinds (table → `Table`, archive → path).
  The "where the source data lands in TEMP_DIR" contract must be clean and predictable.
- **Run + verify** — a specific body runs against the unpacked path (via `${…}` env, the
  same injection seam), then verifies outcomes.
- **Artifact capture (new lifecycle hook)** — sometimes the run's output is stored as an
  artifact for an *external* system to diff against. Needs a clean "where does the
  artifact go / how is it named" contract on teardown.

Design takeaway: keep the kind/instantiator seam general enough that `instantiate()` can
yield either a table (duckdb middleman) or a directory (unpack to TEMP_DIR) — plus an
optional artifact-capture step — **without** bending the common table path around this
case. Most kinds reduce to "a duckdb body"; archive is the deliberate exception, and
the seam should absorb it rather than the reverse.
