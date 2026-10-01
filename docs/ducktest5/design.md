# ducktest5: resources and engines

ducktest5 runs DuckDB tests: `.test` files and Python bodies, on one or more engines (DuckDB, Spark).
The one idea: a test gets its world as `{NAME}` values. ducktest makes what the test needs, gives it
the values, and removes what it made. Part 1 is for a test author and stands alone. Part 2 is for
people who write resource kinds or debug the runner. The rulings behind it are in the maintainers'
notes. Code: `src/ducktest5/` in this repo; the delta example is duckdb-delta, branch
`test/cross-engine-resources`.

---

# Part 1. Writing a test

| Word | What it is | Where you write it |
| --- | --- | --- |
| `{NAME}` | a value the test reads: `{TEMP_DIR}`, `{DATA_DIR}`, your own | SQL; `ctx.env.name` in Python |
| storage | a place for files: a directory, a bucket, a URL | `STORAGES` in `ducktest_conf.py` |
| engine | a thing that runs SQL: DuckDB, Spark | `ENGINES`; `@test(engines=[...])` |
| client | the SQL verbs on one engine, for one test | `ctx.client("duck")` |
| rule | gives tests what they need, all or by file | `RULES`: `default(...)`, `match(...)` |
| Context | the one object a Python body gets | `def body(ctx)` |

## 1.1 A `.test`, with no configuration and on a storage

A tree of `.test` files with no `ducktest_conf.py` runs as it is, on DuckDB's `unittest` binary
(`examples/ladder/01_solo/solo_sqllogic.test`). `{DATA_DIR}` is `data/` at the top of the repository;
`{TEMP_DIR}` (also `__TEST_DIR__`) is the test's own empty place in the run directory. A storage
moves `{TEMP_DIR}` to a directory, a bucket or a URL a service serves; it gives `{DATA_DIR}` only
when it declares `data=`. From `examples/ladder/02_rules/`, `ducktest_conf.py` and `default.test`, shortened:

```python
STORAGES = [StorageDef("scratch_a", f"{SCRATCH}/scratch_a"), StorageDef("scratch_b", f"{SCRATCH}/scratch_b")]
RULES = [default(bind={"storage": "scratch_a"})]
```

```
# -----------------------------------------------------------------------------
# The storage default() binds
#

statement ok
COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/answer.csv'
```

## 1.2 A small Python test

Use Python only where SQL cannot do it: a second engine, threads, a server. SQL stays the language
of the test. From `examples/ladder/02_rules/rule_matched_body.py`, shortened:

```python
@test()
def rule_matched_body(ctx):
    duck = ctx.client("duck")
    duck.oks("COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/t' (FORMAT csv)")
    duck.expects("SELECT answer FROM read_csv('{TEMP_DIR}/t')", [(42,)])
```

| Verb | Like | Meaning |
| --- | --- | --- |
| `oks(sql)` | `statement ok` | must succeed |
| `errs(sql, text)` | `statement error` | must fail, with `text` in the message |
| `expects(sql, rows)` | `query` + `----` | the rows, as tuples or TAB-separated text |
| `setup(sql)`, `teardown(sql)` | | not under test; a failure is an ERROR; teardown runs always |

A failure stops the test. SQL is a plain string: an f-string is a scan error that says what to do.
Names are case-insensitive: `ctx.env.auth` in Python is `{AUTH}` in a `.test`. Give your own with
`ctx.env.set(name=...)`, a rule's `values=` or a verb's keyword; a params row and axis values are values too.

A body can also wrap a `.test` of the same name: Python does what SQL cannot, then `ctx.run_paired()`
runs the file through the binary, with the same env. From `examples/ladder/03_credentials/paired_scoped_secret.py`:

```python
@test()
def paired_scoped_secret(ctx):
    ctx.client("duckdb").expects("SELECT name, type, scope FROM duckdb_secrets()", SECRETS[ctx.env.auth])
    ctx.run_paired()  # runs paired_scoped_secret.test
```

## 1.3 Two engines

The one DuckDB engine of a project is every test's. `@test(engines=[...])` names more; each gets a
client, all see one `{TEMP_DIR}`. From delta `test/cross_engine/create_by_path.py`, shortened:

```python
@test(engines=["duck", "spark"])
def duckdb_creates_by_path_spark_appends_by_path(ctx):
    spark = ctx.client("spark")
    # ... ctx.client("duck") attaches '{TEMP_DIR}/people', creates the table, inserts (1, 'duck')
    spark.oks("INSERT INTO delta.`{TEMP_DIR}/people` VALUES (2, 'spark')")
    spark.expects(
        "SELECT id, name FROM delta.`{TEMP_DIR}/people` ORDER BY id",
        rows="""
        1	duck
        2	spark
        """,
    )
```

## 1.4 Running

Run from above your tests (`ducktest5` finds `./test`, else `.`). The DuckDB build is found at the
top of the repository, else set `BUILD_DIR`. Paths narrow each command; `--id ID` runs one test.
A path also takes the `.test_slow` files under it; `--suite default` leaves them out, as a bare run does.
From `examples/ladder/02_rules`, shortened (`--no-binary-check`: the build does not list the ladder):

```
$ ducktest5 scan --env              # what each test gets, and from where
rule_matched_body.py::rule_matched_body
  root: temp_dir_root=/tmp/ducktest-ladder/scratch_a/temp/<run_token>  <- storage scratch_a
$ ducktest5 select --explain        # what a run takes, which rule gave what, and why not
run   own_inputs.py::own_inputs
        bind={'storage': 'scratch_b'}  <- @test
skip  excluded.test::excluded  -- not in suite 'default': declared out  <- ducktest_conf.py: match 'excluded.test'
$ ducktest5 run --no-binary-check
5 passed in 2.67s
run dir: <repo>/examples/ladder/02_rules/duckdb_unittest_tempdir/2026-09-30T12-57-31Z--swift-raven-54
```

## 1.5 A failure, and a BLOCKED

A wrong row (in a copy of rung 02), shortened, then rung 04's golden
(`tests/ducktest5_goldens/ladder/04_service.run.txt`):

```
--- wrong_row.py::wrong_row
  ducktest5.resources.client.ExpectationFailed: [duck] wrong result
    expected:
      43
    actual:
      42
[1/3] BLOCKED  blocked.test::blocked  [nowhere did not come up]
```

A failure also prints the query, `kept:` (what is left to look at) and `again: ducktest5 run --id ...`.
A name with no value is an ERROR that lists the test's names. BLOCKED: a service did not come up.

## 1.6 Moving an existing repo

1. Run your `.test` tree with no `ducktest_conf.py` (1.1): from the top of the repo,
   `ducktest5 run test/sql --suite default` (rung 00).
2. Extensions the tests need go on the engine: `duckdb_engine("duck", requires=[...])`.
3. Scripts that export keys become a `CredentialDef` with `from_env` (2.5). CI keeps its secrets.
4. `docker compose` services become `SERVICES`; one that runs already is `.as_existing(address=...)`.
5. CI steps that start, wait, run and stop become one `ducktest5 run`.

---

# Part 2. How it works

## 2.1 Resources

Every kind has one shape: a `FooDef` declared once, a `Foo` instance the run makes from it. A Def
has **properties** (what the author declares, known before anything exists, part of identity: a
storage's root, an engine's `requires`), **inputs** (the Defs it consumes; a bare id is read-write,
`{"id": "ro"}` read-only) and makes **attributes** (what the world decides after make: a port,
`http.url`; never identity). Secrets are attributes on their own channel, never written to a file.

| Kind | Instance is | Inputs | Attributes |
| --- | --- | --- | --- |
| `StorageDef` | a directory, a bucket, a served URL | a service, or none | `TEMP_DIR`, `DATA_DIR`, `LOCAL_*`, `S3_ENDPOINT` |
| `CredentialDef` | a secret from the first source that resolves | a service that makes it, or none | secrets only |
| `ServiceDef` | a thing that runs at an address | storages, credentials | `<id>.<name>` (`http.url`) |
| `EngineDef` | a service with a live SQL handle | the test's storages, credentials | its preamble, clients |

The handler contract, for a kind author: `create` (adopts what exists), `read`, `delete` (of an
absent thing succeeds), `list(run_token)`, `ready`; optional `start`/`stop`, `alloc`/`dealloc`,
`expires_at`/`renew`. `acquire(mode, key)` shares the instance, locks it, or mints a child for `key`.
`retain` is `never | on_failure | always`. `check_contract` in `resources/contract.py` tests a kind.
A Test is a consumer, not a resource: its params row and axis values are its properties, its
bindings its inputs, and it has no handlers.

## 2.2 Engines, clients, Context

The Python objects are thin handles all the way down: the engines run elsewhere. A DuckDB engine is
a `duckdb` shell process, Spark is a JVM (one clone per worker), a `.test` batch is a `unittest`
process. An engine instance serves one test at a time and is reset between tests (DuckDB: a fresh
instance and its preamble; Spark: its conf restored); after a failed test it is opened again.
`lifetime` is `worker` (default), `batch` or `run`. `requires`, `settings`, `init` and `process_env`
are part of its identity; `lifetime` and `timeout` are not.

A **client** is one engine's lease for one test: the verbs, the lock, the engine's dialect. The
**Context** (`ctx`) is the one object a body gets: its clients, `ctx.env`, `ctx.params`, and what
it gathers (failed checks, teardowns, the statement log). It is never declared. `ctx.client("duck",
new=True)` is an extra client of the same engine with a shell of its own, for threads that must run at
once. `ctx.run_paired()` hands the body's `.test` twin to the binary.

## 2.3 Binding and env

Two things reach a test. **Roles** (`storage`, `credential`) bind inputs by id, with `bind=`. That
is all `bind=` does. **Values** are the `{NAME}`s. Both merge in one order: the root
`ducktest_conf.py`, each file down to the test, then `@test`. Dict fields update by key, list
fields (`engines`, `tags`) add.

| Source | Roles | Values |
| --- | --- | --- |
| rules, then `@test` (later replaces) | `default(bind=)`, `match(bind=)`, `@test(bind=)` | a rule's `values=` |
| params row | | `@test(params=...)` |
| axis value | a Variant's `inputs`; replaces only `default()` | a Variant's `values`, `{auth}` = its id |
| at run time | | `ctx.env.set(...)`, then `client.env.set(...)`, then the verb's keywords |

An axis value and a rule, `@test` or the params row that give the same role or the same value is a
scan error naming both; so are two axis values that give one name. Only `default()` gives way. Templates fill in two steps: a value that holds `{NAME}`s is filled
first from the other values (a cycle is an error), then the SQL in one pass. A secret is never a value.

## 2.4 Storages, temp levels, tokens

One `run_token` per invocation (`2026-09-30T12-57-31Z--swift-raven-54`); one `batch_token` per
`unittest` process (the binary's `RUN_ID`). A storage has three temp levels:

| Level | Where |
| --- | --- |
| root | `StorageDef("scratch_a", "/tmp/ducktest-ladder/scratch_a")` |
| run | `<root>/temp/<run_token>`: `list(run_token)` finds all a run made |
| `TEMP_DIR` | Python: `<run>/<key>`; `.test`: `<run>/<batch_token>/<test id>`, made by the binary |

`default_storage()` is the implicit default: `DATA_DIR` = `<repo>/data`, `TEMP_DIR` = the test's own
place in the run dir (`duckdb_unittest_tempdir/<run_token>/tests/<key>` for Python). A project may
name it in `STORAGES`. At run end the run level goes, except the places of failed tests.

## 2.5 Credentials

A `CredentialDef` has `scopes` (storage ids, or `(id, "ro")`) and `sources`, one ordered list tried at
acquire: `from_env({src: dst})`, `"env:NAME"`, a callable, and `"1password:NAME"` (planned; refused
now). A named credential wins; else the one scoped to the storage; several need one `default=True`.
A test that unsets every name a credential delivers (`process_env={"AWS_ACCESS_KEY_ID": None}`) does
not get it. Delivery is in the engine's dialect: DuckDB `CREATE SECRET ... SCOPE`; for `unittest` the
same statement with `{AWS_ACCESS_KEY_ID}`, filled by the binary from variables passed by name; Spark
per-bucket `fs.s3a` conf. A service can make one: `s3.credential("keys", scopes=["lake"])`.

## 2.6 Services

A service runs; a test reaches it through a storage on it (`StorageDef("served", "{http.url}",
inputs=[HTTP])`), a credential it makes, or `bind=`. Kinds: `HttpServerDef`, `S3LocalDef` (a local S3
server, RustFS today, the author does not care; `s3.storage("lake", "prefix")` declares
a storage on it), and the engines. The run starts each needed service once, before any test, and stops it after the last.
`.as_existing(address=...)` or `--existing-service ID=URL` is the same service found running: adopted
when it answers, never removed. A service whose kind knows its address before it starts (S3) is
adopted the same way, with no flag, when it already answers there (mode `found running`). The run
reaches services at `DUCKTEST_SERVICE_HOST` and publishes the ones it starts at
`DUCKTEST_SERVICE_BIND` (both `127.0.0.1` by default; HOST follows BIND): the machine with docker sets
BIND, a machine that only reaches the services sets HOST. A service that does not come up makes its
tests BLOCKED.

## 2.7 Phases, identity, batching

`scan` (`01-tests.yaml`) -> `plan` (`02-plan.yaml`) -> `provision` (`03`) -> `exec` (`04-events.jsonl`)
-> `cleanup` -> `report`; `run` does all. Identity is by content: `sha(kind, properties, input
identities, mode)`; a child is `sha(parent, key)`; the id is only a label. A test's identity is its
engine instances plus its env. `.test` files of one identity share one `unittest` batch (an inline
`require` is body, not identity). A Python body is a batch of one; two of one identity may reuse a
warm engine.

## 2.8 What the repo owns

`ducktest_conf.py`: `ENGINES`, `SERVICES`, `STORAGES`, `CREDENTIALS`, `AXES`, `SUITES`, `RULES`; kinds
of its own beside its tests (a catalog service, `UCTable`). ducktest owns lifetime, identity,
delivery, dialects, and the shared kinds.

## 2.9 Words that mean something else in DuckDB or Spark

| Word | Here | Elsewhere |
| --- | --- | --- |
| scope | a credential's storages; nothing else | DuckDB `CREATE SECRET ... SCOPE`: a path prefix |
| env | `ctx.env`: the test's `{NAME}` values | process variables: here `process_env` |
| client | one engine's lease for one test | a DuckDB language binding |
| bind | give a role an input | a prepared-statement parameter; DuckDB's binder |
| Variant | one value of an axis | DuckDB and Delta's `VARIANT` type |

## 2.10 Not built yet

`TableDef` and tables per test; catalog services (UC, Databricks); input slots declared by a kind;
the `1password:` source; scan checks of `{NAME}` references and of
`ctx.client("...")` names; `.test` header blocks as rules; the `Run` object.

---

# Appendix A. The ladder

`examples/ladder/` in this repo; goldens in `tests/ducktest5_goldens/ladder/`.

| Rung | Adds | Path |
| --- | --- | --- |
| 00 | an existing repo's `test/` tree: select by path, `--suite default`, SKIPPED | `00_existing/` |
| 01 | no config: a `.test`, a Python body, and a body paired with a `.test` | `01_solo/` |
| 02 | storages, an engine, `default`/`match`, `@test(bind=)` | `02_rules/` |
| 03 | an s3 credential from env, an `auth` axis | `03_credentials/` |
| 04 | an HTTP service under a storage; BLOCKED | `04_service/` |
| 05 | a local S3 server (RustFS), a storage and a credential on it | `05_s3/` |

# Appendix B. The delta cross-engine tests

`test/cross_engine/` in duckdb-delta, branch `test/cross-engine-resources`: six files, DuckDB and
Spark on one Delta table, each test on local storage and on S3 (`[storage=local]`, `[storage=s3]`).
`test/ducktest_conf.py`, verbatim:

```python
from ducktest5 import Variant, match
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.engines.spark import delta_spark
from ducktest5.resources import default_storage
from ducktest5.services.s3local import s3_local

# The only DuckDB engine is the default one, so the .test files run on it too.
ENGINES = [duckdb_engine("duck", requires=["delta", "json"]), delta_spark(s3a=True)]

# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
S3 = s3_local()

# The default storage: {DATA_DIR} is <repo>/data, {TEMP_DIR} the test's own place in this run.
STORAGES = [default_storage(), S3.storage("s3_lake", "delta")]
CREDENTIALS = [S3.credential("s3_keys", scopes=["s3_lake"])]

AXES = {"storage": [Variant("local", inputs=["default"]), Variant("s3", inputs=["s3_lake"])]}

# The .test files stay on local storage.
RULES = [match("cross_engine/**", variants=["storage"])]
```

A params row and a keyword value, from `test/cross_engine/create_table.py`, shortened:

```python
@test(engines=["duck", "spark"], params=PROPERTY_SETS)
def duckdb_creates_then_spark_and_duckdb_interleave(ctx):
    duck = ctx.client("duck")
    duck.setup("ATTACH '{TEMP_DIR}/t' AS t (TYPE delta)")
    duck.oks(
        """
        CREATE TABLE t.t (i INTEGER, s VARCHAR) {with_properties};
        INSERT INTO t.t VALUES (1, 'duck'), (2, 'duck'), (3, 'duck');
        """,
        with_properties=with_clause(ctx.params),
    )
```

# Appendix C. Next

- **Azure**: `.test` only; Azurite, blob and DFS as a `storage` axis, an `auth` axis. Needs an
  Azurite kind and the `azure` secret dialect.
- **httpfs**: a `storage` axis (http, s3, s3 through a proxy) and two DuckDB engines (curl,
  httplib) named by a Variant's `engines`. Needs a proxy kind.
- **Unity Catalog**: deployments as an axis binding a catalog service and a storage. Needs
  `TableDef`, catalog services and UC CREATE.

# Appendix D. Open decisions

Settled 2026-09-30: pairing (`ctx.run_paired()`) stays, as a basic way to use the runner; the
binary's `--temp-dir-root` keeps its name (the root is where the temp dirs go) and already takes an
`s3://` root (rung 05).

1. TODOs worth a look: S3 is proven live (rung 05, delta `storage=s3`), but the goldens stay BLOCKED; batch dirs of skipped tests stay
   after a green run; scan does not check `ctx.client("...")` names against `engines=`; delta takes the
   runner from a local path (`pyproject.toml`), by decision until a branch is pushed.

# Appendix E. History

- 2026-09-30: `S3LocalDef` (RustFS) replaces `MinioDef`; MinIO is gone; S3 cleanup by rclone or boto3; rung 05.
- 2026-09-30: value conflicts are errors; `ctx.env` holds the roots and axis names; `ctx.client(name,
  new=True)` replaces `ctx.duckdb`; `ctx.run_paired()`; logging with `-v`/`-vv` and `ducktest.log`.
- 2026-09-30: rewritten short, to the code as built: Context and `ctx.env`, `ENGINES`, `lifetime`,
  the default storage, the f-string error; Session gone.
- 2026-09-30: author part first; Engine, Client, Session as built.
- 2026-09-28: owner review: `acquire`, unset by `None`, secrets channel, identity, `sharing`,
  credential scopes, secrets by passthrough.
- 2026-09-27: tables per test; credential scope and delivery.
- 2026-09-25: first version.
