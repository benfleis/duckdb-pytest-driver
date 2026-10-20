# ducktest: resources and engines

ducktest runs DuckDB tests: `.test` files and Python bodies, on one or more engines (DuckDB, Spark).
It needs DuckDB v2.0 or newer; an older build is refused at start, with the version it found.
The one idea: a test gets its world as `{NAME}` values. ducktest makes what the test needs, gives it
the values, and removes what it made. Part 1 is for a test author and stands alone. Part 2 is for
people who write resource kinds or debug the runner. The rulings behind it are in the maintainers'
notes. Code: `src/ducktest/` in this repo. The delta example (Appendix B) is duckdb-delta's conf in the
form of this release; the delta repo carries the earlier form until it is repinned.

---

# Part 1. Writing a test

| Word | What it is | Where you write it |
| --- | --- | --- |
| `{NAME}` | a value the test reads: `{TEMP_DIR}`, `{DATA_DIR}`, your own | SQL; `ctx.values.name` in Python |
| storage | a place for files: a directory, a bucket, a URL | `ducktest_conf.py`; a rule or `@test` names it |
| engine | a thing that runs SQL: DuckDB, Spark | `Project(engines=[...])`; `@test(engines=[...])` |
| client | the SQL verbs on one engine, for one test | `ctx.client("duck")` |
| `duck-shell` | how a DuckDB engine serves a Python body: the `duckdb` CLI | reports: `runner`, `[duck/duck-shell]` |
| `duck-sqllogic` | how a DuckDB engine runs a `.test` batch: today the `unittest` binary | reports: `runner`, the plan's batches |
| rule | gives tests what they need, all or by file | `Project(rules=[...])`, `Rules(...)`: `default(...)`, `match(...)` |
| matrix | parameters, each with named values; a test runs once per cell | `Project(matrix=...)`; `matrix=[...]` on a rule or `@test` |
| name | the label of a resource; a reference takes it, or the object | the first argument: `LocalStorageDef("scratch_a", ...)` |
| path | a place under something rooted: a storage, a bucket, a container | `storage(path=)`, a seed's `(dir, path)`, `STORAGE.at(path)` |
| Context | the one object a Python body gets | `def body(ctx)` |

**Nouns are classes with `Def`, verbs are functions; the long form lives on the class.** A
declaration is a class whose name ends in `Def`: it declares, the run makes the instance
(`S3ServiceDef`, `S3StorageDef`, `S3CredentialDef`, `DuckEngineDef`). A class's makers are on the
class: `S3ServiceDef.rustfs(...)`, `SparkEngineDef.delta(...)`, `Rule.default(...)`,
`Rule.match(...)`. A function is a verb that builds something: `default(...)` and `match(...)` are
the convenience for the two `Rule` makers, and `bucket`, `container`, `from_env`, `default_storage`,
`test` and `skip` the others. A service class names the protocol and the role; its `flavor` says what
backs it (`S3ServiceDef(name, flavor="rustfs")`). A storage class names its protocol. A conf imports
all of them in one line, `from ducktest import ...`.

## 1.1 A `.test`, with no configuration and on a storage

A tree of `.test` files with no `ducktest_conf.py` runs as it is, on DuckDB's `unittest` binary
(`examples/ladder/01_solo/solo_sqllogic.test`). `{DATA_DIR}` is `data/` at the top of the repository;
`{TEMP_DIR}` (also `__TEST_DIR__`) is the test's own empty place in the run directory. Three slots
bind a storage, a directory, a bucket or a URL a service serves, and each says one thing:

| slot | means | gives |
| --- | --- | --- |
| `storage=` | the storage the test is about | its values (`{S3_ENDPOINT}`, `{AZ_ACCOUNT}`, ...); nothing about roots |
| `data=` | where fixtures are read | `{DATA_DIR}`: the storage's root |
| `temp=` | where the test writes | `{TEMP_DIR}` under `<root>/temp/<run token>`, which is `{TEMP_DIR_ROOT}` |

`data=` and `temp=` also take `STORAGE.at("sub/path")`, a place under a storage: `data=` reads
`<root>/sub/path`, `temp=` writes under `<root>/sub/path/<run token>`. Where nothing binds one of
them, it is the default's: `data/` and the run directory. A credential scoped to a storage covers it
whichever slot bound it. From `examples/ladder/02_rules/`, `ducktest_conf.py` and `default.test`, shortened:

```python
SCRATCH_A = LocalStorageDef("scratch_a", f"{SCRATCH}/scratch_a")
SCRATCH_B = LocalStorageDef("scratch_b", f"{SCRATCH}/scratch_b")
DUCK = DuckEngineDef("duck", requires=["parquet"])

project = Project(engines=[DUCK], rules=[default(storage=SCRATCH_A, temp=SCRATCH_A)])
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
Names are case-insensitive: `ctx.values.auth` in Python is `{AUTH}` in a `.test`. Give your own with
`ctx.values.set(name=...)`, a rule's `values=` or a verb's keyword; the values of a cell are values too (1.7).

A body can also wrap a `.test` of the same name: Python does what SQL cannot, then `ctx.run_paired()`
runs the file through the binary, with the same values. From `examples/ladder/03_credentials/paired_scoped_secret.py`,
with the engine `duck` of 1.1 (rung 03 declares none; the built-in one is `duck` too):

```python
@test()
def paired_scoped_secret(ctx):
    keys = ctx.credential("fake_key")
    ctx.client("duck").expects("SELECT name, type, scope FROM duckdb_secrets()", [(keys.secret_name, keys.type, keys.scope)])
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

Run from above your tests (`ducktest` finds `./test`, else `.`). The DuckDB build is found at the
top of the repository, else set `BUILD_DIR`. Paths narrow each command; `--id ID` runs one test.
A path also takes the `.test_slow` files under it; `--suite default` leaves them out, as a bare run does.
From `examples/ladder/02_rules`, shortened:

```
$ ducktest scan --env              # what each test gets, and from where
rule_matched_body.py::rule_matched_body
  root: temp_dir_root=/tmp/ducktest-ladder/scratch_a/temp/<run_token>  <- storage scratch_a
$ ducktest select --explain        # what a run takes, which rule gave what, and why not
run   own_inputs.py::own_inputs
        storage='scratch_b'  <- @test
        temp='scratch_b'  <- @test
skip  excluded.test::excluded  -- not in suite 'default': tag opt-in
$ ducktest run
9 passed, 1 skipped in 2.67s
run dir: <repo>/examples/ladder/02_rules/ducktest_temp/2026-09-30T12-57-31Z--swift-raven-54
```

Tests carry tags (`tags=[...]` on a rule or `@test`); a `.test_slow` file has the tag `slow`, a
`.test_coverage` file `coverage`. A bare run is the suite `default`: every test but those tagged
`slow`, `coverage` or `opt-in`. Give `opt-in` to a test a bare run should leave out (it reads the
public network, it is manual); `--suite all` runs everything. `Project(suites=...)` in the root
`ducktest_conf.py` names selections over tags and paths, in the command line's own words:

```python
project = Project(suites={"smoke": {"paths": ["sql/core"], "select": ["tag=fast"], "skip": ["tag=spark"]}})
```

`--select`, `--skip` and a suite's `select`/`skip` take `key=value` terms (`tag`, `type`, `path`,
`suite`, a parameter's name: `storage=s3`); `!` negates a term: `--select '!tag=spark'`,
`--select 'suite=smoke;!tag=s3'`.

## 1.5 A failure, and a BLOCKED

A wrong row (in a copy of rung 02), shortened, then rung 04's golden
(`tests/ducktest_goldens/ladder/04_service.run.txt`):

```
--- wrong_row.py::wrong_row
  ducktest.resources.client.ExpectationFailed: [duck/duck-shell] wrong result
    expected:
      43
    actual:
      42
[1/3] BLOCKED  blocked.test::blocked  [nowhere did not come up]
```

A failure also prints the query, `kept:` (what is left to look at) and `again: ducktest run --id ...`.
A name with no value is an ERROR that lists the test's names. ERROR: the test started, and the
harness or an engine broke under it. BLOCKED: the test never started, because an input it binds did
not resolve: a service that did not come up, a credential with no source, a storage that was refused.
A run exits 0 only when no test is FAILED, ERROR or BLOCKED and at least one PASSED, so a run where
every test is SKIPPED is red.

A Python body ends SKIPPED with `ctx.skip("duckdb CLI not built")`, or `ducktest.skip(...)` from a
helper that has no `ctx`. The report shows the reason, as for a `require` the binary did not meet.
Skip only on a condition the body checks itself (a binary not built, a feature off in this build); a
service, an engine or a credential is declared, and when it does not come up the test is BLOCKED. A
skip after statements ran keeps their log; a check that failed before it makes the test FAILED. When
the binary skips the file that `ctx.run_paired()` runs, the body ends SKIPPED with the binary's reason.

## 1.6 Moving an existing repo

1. Run your `.test` tree with no `ducktest_conf.py` (1.1): from the top of the repo,
   `ducktest run test/sql --suite default` (rung 00). For Python bodies, `ducktest init` writes
   the repo's test environment, `pyproject.toml`, with the runner pinned (README, Install); it also
   makes sure `.gitignore` ignores the runner's default temp root.
2. Extensions the tests need go on the engine: `DuckEngineDef("duck", requires=[...])`.
3. Scripts that export keys become a typed credential, such as `S3CredentialDef` with `from_env`
   (2.5). CI keeps its secrets. Keys of the shell's own env no longer reach the engines by
   themselves: declare `S3CredentialDef.from_aws_env(...)` where a test means to use them.
4. `docker compose` services become service declarations (`S3ServiceDef.rustfs(...)`), which a test reaches
   through a storage or a credential on them, or `service=`; one that runs already is `.as_existing(address=...)`.
5. CI steps that start, wait, run and stop become one `ducktest run`.
6. Helper code outside the test files' directories (`test/py`, `scripts`) goes on the path in the
   repo's `pyproject.toml`, `[tool.ducktest] pythonpath = ["test/py", "scripts"]`, relative to it, so
   `ducktest_conf.py` itself can import from it (2.8).
7. Files gated one by one with `require-env NAME` (httpfs gates its S3 tests so) can be given what
   they need by that line:
   `match("**", requires_env="S3_TEST_SERVER_AVAILABLE", storage="lake", values={"S3_TEST_SERVER_AVAILABLE": "1"})`
   selects the `.test` files that hold `require-env S3_TEST_SERVER_AVAILABLE`, with or without a
   value, among those the pattern matches. `requires_env=` only selects: unless a value, a `process_env=`
   entry or a bound credential's env gives the name, the file's own `require-env` line is not met
   and the file is SKIPPED, and the scan warns. Deprecated
   from the start: it is a way in, not a way to stay; move the files to a directory or give them
   tags. The scan says so once, at `-v`.

## 1.7 A matrix

A test can run more than once: once per **cell** of its **matrix**. A matrix is **parameters** (one
dimension each: `storage`, or a test's own `case`), and each parameter has named **values** (`s3`,
`varchar`). A cell is one value of every parameter: what runs, and what other tools call a variant.
Its id names each value, `key=value`:
`duckdb_creates_partitioned_spark_reads_and_writes[storage=s3,case=varchar]`.

Parameters that several tests share are declared in the root `ducktest_conf.py`, as
`Project(matrix=...)`. A value is a dict of slots, the options of 1.8; a name or a declaration is the
short form for a parameter named after a slot (`storage`, `data`, `temp`, `credential`, `service`),
and fills that slot only. A rule or `@test` names the parameters a test runs over with `matrix=[...]`.
A cell is one more rule, applied last, that binds the slots its value names; the test's roots follow
from whatever that rule binds, as from any other: a value that binds `storage=` alone leaves
`{TEMP_DIR}` where it was. From delta's `test/ducktest_conf.py`, shortened:

```python
project = Project(
    matrix={
        "storage": {
            "local": {"storage": LOCAL_DEFAULT, "temp": LOCAL_DEFAULT},
            "s3": {"storage": LOCAL_LAKE, "temp": LOCAL_LAKE},  # the tables go under {TEMP_DIR} on the server
        }
    },
    rules=[match("cross_engine/**", matrix=["storage"])],
)
```

A test can define parameters of its own in the same list, as a dict: `{"case": CASES}`. Their values
are free data for the body, not slots. From delta's `test/cross_engine/partitioned.py`, shortened:

```python
CASES = {
    "varchar": {"columns": "i INTEGER, p VARCHAR", "partition_by": "p", "filter": "p = 'a'", ...},
    "integer": {...},
}

@test(engines=["duck", "spark"], matrix=[{"case": CASES}])
def duckdb_creates_partitioned_spark_reads_and_writes(ctx):
    case = ctx.params.case                    # this cell's value of `case`: one row of CASES
    ctx.client("duck").oks("CREATE TABLE t.t ({columns}) PARTITIONED BY ({partition_by})")
    ctx.client("spark").expects("SELECT i FROM delta.`{TEMP_DIR}/t` WHERE {filter} ORDER BY i", rows=case["filter_ids"])
```

The rule gives `storage` (2 values), the test `case` (6): 12 cells. `ctx.params` holds every
parameter of the cell by name, `ctx.params.case` or `ctx.params["case"]`: the data of a test's own
value, the name of a value of the project's matrix (`ctx.params.storage` is `"s3"`). The values of a
cell are `{NAME}`s: a project parameter's name is its value's name (`{storage}`); the fields of a
test's own value are values (`{columns}`), never bindings (a binding slot as a field is a scan
error: bind the project's parameter or `@test(storage=)`), and a value that is no dict is
`{case}` itself. Reports name a value of either kind by its parameter, `matrix storage=s3` or
`matrix case=varchar`.

An id names the parameters in the order the matrix gives them: the rules' from the root
`ducktest_conf.py` down, then `@test`'s, each where it is first named. A rule can define a parameter
of its own too. Two parameters of one name are a scan error, and so are a parameter of a test's own
named as one of the project's, and two parameters (or a parameter and a rule) that give one `{NAME}`.

## 1.8 Options

A rule (`default(...)`, `match(pattern, ...)`), `@test(...)` and a value of the project's matrix take
one set of options, the slots. They merge from the root `ducktest_conf.py` down, then `@test`, then
the matrix values of the cell. An unknown name is a scan error that lists the ones there are. A
`match()` pattern is a path relative to its conf file (`*` inside one directory, `**` across); a body
paired with a `.test` matches by either file's path, so `match("sql/*.test")` reaches it too.

| Option | What it gives | Merge |
| --- | --- | --- |
| `storage=`, `credential=`, `service=` | a storage, a credential, a service, which the project then declares: its name or the object (2.3); `storage=` gives the storage's values | a later one replaces |
| `data=`, `temp=` | a storage or `STORAGE.at("sub/path")`: where `{DATA_DIR}` is, where `{TEMP_DIR}` is made (1.1) | a later one replaces |
| `values={...}` | `{NAME}`s | by name |
| `requires=[...]` | extensions to load first | add up |
| `settings={...}` | DuckDB settings, as `SET` after the requires | by name |
| `init_sql="..."` | statements after the settings; never printed, only its source | add up |
| `process_env={...}` | the engine process's variables; `None` unsets one | by name |
| `engines=[...]` | the engines the test drives, by name or the object | add up |
| `matrix=[...]` | the parameters the test runs over (1.7; not on a value of the project's matrix) | add up |
| `tags=[...]` | what selects it; `opt-in` keeps it out of a bare run | add up |
| `mutex="name"` | what the test must have to itself while it runs | add up |
| `timeout=N` | seconds for the whole test; past it, ERROR | a later one replaces |
| `retries=N` | runs again, alone, after a FAILED or ERROR | a later one replaces |

What a slot names is declared by that: a rule, a matrix value or `@test(...)` names exactly what the
test needs, and the run declares it, with what it pulls (1.9). A `{name.attr}` in `values=`,
`settings=` or `init_sql=` does not bind `name`: it reads an input the test holds, bound or pulled
through its chain (a storage's service, a credential's service), and a template that names anything
else is a scan error. The run fills it only in `values=`.

A value of a cell gives way to nothing: a value or binding it gives that a rule, `@test` or another
parameter of the cell gives too is a scan error; only what `default()` binds gives way to it. That
holds per slot: a cell's `temp=` and a `match(temp=...)` on the same test are an error, a cell's
`temp=` and a `match(storage=...)` are not.

## 1.9 The conf file: `Project`, `Rules`, names

The root `ducktest_conf.py` holds one `Project(...)`, in a variable of any name; a file below it holds
the rules for the tests below it, `rules = Rules(default(...), match(...), ...)`. The loader finds them
by a scan of the module's top-level names; two in one file, a `Project` below the root, or a `Rules`
in the root (its rules go in `Project(rules=[...])`) is a scan error that says what to write instead;
a file with neither is a warning. `Project` takes keywords only, each optional: `engines`,
`matrix`, `suites`, `inherit_env`, `rules`, `pythonpath`, and `services`, `storages`, `credentials`
(`help(Project)` lists them). A misspelled one fails where it is written: `Project(credential=...)`
is a `TypeError` at import, and a type checker says "did you mean `credentials`".

**What a project declares is what its tests reach.** A rule, a matrix value or a body's `@test(...)`
names what a test needs (`temp=LOCAL_LAKE`), by the object or by its name, and that is declared: a
storage brings its service and every credential whose `scopes=` names it; a credential brings the
service it is made of; `SERVICE.storage(...)` is on that service. A rule reaches only from a
`rules=`: one made and held by no `Project(rules=)` or `Rules(...)` gives nothing, and warns. A
`{name.attr}` template reaches what it names, but the test must also hold it (1.8). So a conf
writes no list of its services, storages or credentials. `services=`, `storages=` and `credentials=` stay, as additions:
what they list is declared with what it pulls, and the scan warns when nothing reaches it.
`engines=` is different, and needed for every engine but the built-in one: an engine is not reached
through what a test binds, and the project's one DuckDB engine is the default of every test only
when `engines=` lists it. From `examples/ladder/05_s3/ducktest_conf.py`:

```python
from ducktest import DuckEngineDef, Project, S3ServiceDef, default

LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="lake")
LOCAL_KEYS = LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])

project = Project(
    engines=[DuckEngineDef("duck", requires=["httpfs"])],
    rules=[default(storage=LOCAL_LAKE, temp=LOCAL_LAKE)],  # temp=: {TEMP_DIR} is on the server
)
```

The rule reaches `local_lake`, which brings `local_s3` and `local_keys`.

**Declarations are in conf files.** A test file declares nothing: a service, storage, credential,
engine, bucket or container made while a body is imported, or by a helper module it imports, is a
scan error at that line. A body binds what the conf declares by name, `@test(storage="local_lake")`;
a body that imports `ducktest_conf.py` is told so. `ducktest cleanup` reads no body: it takes the
services and storages a run used from the run directory (`03-provision.yaml`, and each test's needs
in `01-tests.yaml`), makes each from the conf files by name, and runs none of these checks.

**A storage's credential, by the mode the test needs.** A test that holds a storage only through
`data=` reads it; through `storage=` or `temp=` (also beside `data=`) it writes it. Of the
credentials whose `scopes=` cover the storage, those whose scope allows that mode are the
candidates (an `rw` scope allows both). Of several, the ones scoped with exactly that mode win, so a
pair `(LOCAL_LAKE, "ro")` and `LOCAL_LAKE` gives a `data=` test the first and a `temp=` test the
second; of several still, `default=True` picks one, else the scan says which and how to choose
(`credential=` on the rule, or `default=True`). A credential named with `credential=` is used as
given. `ro` is ducktest's word for which credential to pick; it does not restrict the keys.

**Names.** Every resource has a name, its first argument: `S3ServiceDef.rustfs("local_s3")` (`s3`
when left out), `LOCAL_S3.storage("local_lake", ...)`, `LOCAL_S3.credential("local_keys", ...)`,
`DuckEngineDef("duck")`. It is a label you pick; what a resource is (its identity) is its content,
so two names may stand for one identity. The examples keep UPPER_CASE variables and lowercase names,
each saying its domain and role (`LOCAL_LAKE`, `"local_lake"`), never a bare `S3` or `LAKE`.
Engines, services, storages and credentials share one namespace, the one a value's `{name.attr}`
reads (2.3); a bucket or a container is named within its server. `duck` is the built-in DuckDB
engine's name: a `DuckEngineDef("duck")` may take it, a storage, credential or service may not. A
reference slot takes the name or the object itself: `storage=`, `data=`, `temp=`, `credential=`,
`service=`, `engines=[...]`, `scopes=[...]`, `container=`, `bucket=`, and a value of the project's
matrix. The object gives an editor's go-to-definition from a rule to what it binds; either way the
run sees the name.

**Checks.** At scan, over every conf file of the tree and every body (not only what a run
selects). They run once the bodies are imported, on the declared set, so what only a body names
counts; a body that fails to import is told first, before an error of the conf's names:

| What | |
| --- | --- |
| two declarations of one name | error |
| a reference to a name nothing declares | error, with the closest name |
| `{name.attr}` in `values=`, `settings=` or `init_sql=`, with an attribute the kind of `name` does not have | error, with the ones it has |
| `{name.attr}` that names what the test does not hold, bound or through its chain | error, naming the test and how to bind it |
| a Def made in a test file or a helper it imports; a body that imports `ducktest_conf.py` | error, at that line |
| a storage with several credentials for the mode the test needs, none `default=True`; a storage a test writes whose credentials are all scoped `ro` | error, naming the test, each credential's line and the fix |
| a reference to an engine `engines=` does not list | error, naming where to list it |
| a container not given to `AzureServiceDef(containers=...)`, a bucket not given to `S3ServiceDef(buckets=...)` | error, naming where to give it |
| a credential scoped to a storage of another scheme; a storage its service cannot serve | error |
| `STORAGE.at(...)` in a slot other than `data=`/`temp=`, or on the default storage; a path with `..` or `/` at the start | error |
| a declaration nothing reaches; one `services=`, `storages=` or `credentials=` lists that nothing reaches; a listed engine no test drives; a matrix parameter no rule or test runs over; a rule in no `rules=` | warning; `--strict` (scan, select, run) makes it an error |

---

# Part 2. How it works

## 2.1 Resources

Every kind has one shape: a `FooDef` declared once, a `Foo` instance the run makes from it. A Def
has **properties** (what the author declares, known before anything exists, part of identity: a
storage's root, an engine's `requires`), **inputs** (the Defs it consumes; a bare name is read-write,
`{"name": "ro"}` read-only) and makes **attributes** (what the world decides after make: a port,
`http.url`; never identity). Secrets are attributes on their own channel, never written to a file.

| Kind | Instance is | Inputs | Attributes |
| --- | --- | --- | --- |
| `StorageDef` | a directory, a bucket, a served URL | a service, or none | `S3_ENDPOINT`, `S3_USE_SSL`, `AZ_ACCOUNT`; its places give `DATA_DIR`, `TEMP_DIR`, `LOCAL_*` |
| `S3CredentialDef`, `AzureCredentialDef` (a `CredentialDef`) | fields from the first source that resolves | a service that makes it, or none | its fields, as SDK env |
| `ServiceDef` | a thing that runs at an address | storages, credentials | `<name>.<attribute>` (`http.url`) |
| `EngineDef` | a service with a live SQL handle | the test's storages, credentials | its preamble, clients |

The handler contract, for a kind author: `create` (adopts what exists), `read`, `delete` (of an
absent thing succeeds), `list(run_token)`, `ready`; optional `start`/`stop`, `alloc`/`dealloc`,
`expires_at`/`renew`. `acquire(mode, key)` shares the instance, locks it, or mints a child for `key`.
`retain` is `never | on_failure | always`. `check_contract` in `resources/contract.py` tests a kind.
A Test is a consumer, not a resource: the values of its cell are its properties, its bindings its
inputs, and it has no handlers.

## 2.2 Engines, clients, Context

The Python objects are thin handles all the way down: the engines run elsewhere. A DuckDB engine is
one build. It runs a `.test` batch as `duck-sqllogic` (today the `unittest` binary) and serves a
body as `duck-shell` (the `duckdb` CLI); both get the engine's extensions and settings. The plan,
the events (`runner`) and a failure name the one that ran (`[duck/duck-shell] statement failed`),
and `scan --env` the one a test would run on; a test's `type` stays what the test is. Spark is a
JVM (one clone per worker). An engine instance serves one test at a time and is reset between
tests (DuckDB: a fresh database in the same shell process, and its preamble; Spark: its conf
restored); after a failed test it is opened again.
`lifetime` is `worker` (default), `batch` or `run`. `requires`, `settings`, `init_sql` and `process_env`
are part of its identity; `lifetime` and `statement_timeout` (for one statement) are not. Tests one after the other share an
instance of one identity; two names for engines of one identity in one test get two instances. A
test takes its mutexes, then its engine instances, each in one order for the whole run, so tests on
several workers never wait for each other in a circle. A Python body past its timeout is ERROR, and
what it holds is released at once; its thread cannot be killed and is left to end on its own, while a
new one takes its worker slot.

A **client** is one engine's lease for one test: the verbs, the lock, the engine's dialect. The
**Context** (`ctx`) is the one object a body gets: its clients, `ctx.values`, `ctx.params`, and what
it gathers (failed checks, teardowns, the statement log). It is never declared. `ctx.client("duck",
new=True)` is an extra client of the same engine with a shell of its own, for threads that must run at
once. `ctx.run_paired()` hands the body's `.test` twin to the binary.

## 2.3 Binding and values

Two things reach a test. **Bindings** name inputs, by name or by the object, one keyword per slot:
`storage=`, `data=`, `temp=` (one of the project's `storages`; `data=` and `temp=` also take
`STORAGE.at("sub/path")`), `credential=` (one of `credentials`), `service=` (one of `services`). A
slot is a kind and a role: the three storage slots bind one kind, and what the storage gives the test
follows the slot (1.1). **Values** are the
`{NAME}`s. Both merge in one order: the root `ducktest_conf.py`, each file down to the test, then
`@test`, then the values of the test's cell. Dict fields update by key, list fields (`engines`,
`tags`) add.

| Source | Bindings | Values |
| --- | --- | --- |
| rules, then `@test` (later replaces) | `default(storage=)`, `match(credential=)`, `@test(temp=)` | a rule's `values=` |
| a value of the project's matrix | `{"storage": ..., "temp": ...}`; replaces only `default()` | its `values`; `{storage}` = its name |
| a value of a test's own parameter | | its fields, or `{case}` = the value |
| at run time | | `ctx.values.set(...)`, then `client.values.set(...)`, then the verb's keywords |

A value may name an attribute of an input the test holds, or of an input of one, as `{name.attr}`:
`values={"AWS_ENDPOINT": "{local_s3.host_port}"}` in a test that holds a credential the `local_s3` service makes.
It is filled when the test runs; `scan --env` shows it as written. The scan checks that the name is
declared and that its kind has the attribute (1.9). A rule's `requires=["httpfs"]` loads an extension
first, as a matrix value's does, so its `settings` can name a setting of it.

A value of a cell and a rule or `@test` that bind the same slot or give the same value is a scan
error naming both; so are two values of a cell that give one name. Only `default()` gives way. Templates fill in two steps: a value that holds `{NAME}`s is filled
first from the other values (a cycle is an error), then the SQL in one pass. A secret is never a value.

## 2.4 Storages, temp levels, tokens

One `run_token` per invocation (`2026-09-30T12-57-31Z--swift-raven-54`); one `batch_token` per
`unittest` process (the binary's `RUN_ID`). The storage `temp=` binds has three temp levels:

| Level | Where |
| --- | --- |
| place | `<root>/temp`, or `<root>/<path>` for `temp=STORAGE.at(path)` |
| run | `<place>/<run_token>`, `TEMP_DIR_ROOT`: `list(run_token)` finds all a run made |
| `TEMP_DIR` | Python: `<run>/<key>`; `.test`: `<run>/<batch_token>/<test id>`, made by the binary |

`DATA_DIR` is the root of the storage `data=` binds, or `<root>/<path>` for `data=STORAGE.at(path)`;
the same in every run, never removed. The place of `temp=STORAGE` is the storage's `temp_path`
(`temp` by default). `StorageDef` has no `data=`: where fixtures are read is a
binding, so one bucket can be one test's fixtures and another's scratch. `default_storage()` is the
implicit default of both slots: `DATA_DIR` = `<repo>/data`, `TEMP_DIR` = the test's own place in the
run dir (`ducktest_temp/<run_token>/tests/<key>` for Python). A matrix value or a rule may name it,
`default_storage()`, as any storage; it has no places (`--data-dir`, `--temp-dir-root` move it). At run end the run level of every place
goes, except the places of failed tests; a place is known to cleanup once a conf asks for it.

## 2.5 Credentials

A credential's SDK env names are used even where no file names them: an SDK reads
`AWS_SECRET_ACCESS_KEY` with `getenv()` inside the engine. So the runner never decides delivery,
pruning, batching or identity from whether a name appears in a `.test` or a body; the conf declares
the credentials, and holding one is what lets a test run. The runner delivers them by process env to
every engine process of the test, removes SDK names that come from the shell's env, and lists both in
`scan --env` and in a failure's made-of.

**Typed.** A credential is declared as a typed `FooDef` (a `CredentialDef`); the run makes a `Foo` (a
`Credential`) from it. `S3CredentialDef` has the fields `key_id`, `secret`
(both required and secret), `session_token` (secret) and `region`, DuckDB's secret options in lower
case, and their SDK env names (`AWS_ACCESS_KEY_ID`, ..., `region` as `AWS_REGION` and
`AWS_DEFAULT_REGION`). Its `provider` is `credential_chain` (the default) or `config`; another is a
scan error. A bare `CredentialDef` is a scan error.

`AzureCredentialDef` takes DuckDB's azure secret providers as
`provider`: `config` (`connection_string`, or only `account_name` for anonymous reads),
`service_principal` (`tenant_id`, `client_id`, `client_secret` or `client_certificate_path`),
`credential_chain` (with `chain`: `'env'`, `'cli'`, `'managed_identity'`, `'cli;env'`, ...) and
`access_token`. Each provider says what a source must give; with `chain="env"` that is the service
principal. Its SDK env names are `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_ACCOUNT`,
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_CLIENT_CERTIFICATE_PATH` and
`AZURE_ACCESS_TOKEN`: the names the SDK's environment credential reads, and those the azure
extension's `.test` files read. `AzureCredentialDef.from_azure_env(name, provider=...)` takes the
shell's own. Its secret is `TYPE azure` with the provider's keys by env name, `ACCOUNT_NAME` from
the storage (an abfss root names it) and `SCOPE`; `credential_chain` keeps its keys in the env, where
the SDK reads them. Spark renders no Azure secret: a Spark test that scopes one is a scan error.

**Sources.** `sources` is one ordered list, tried at acquire; the first that resolves gives every
field, and sources never mix. `from_env({"MY_KEY_ID": "key_id", "MY_SECRET": "secret"})` reads each
variable into a field; `from_env([...])` and `"env:NAME"` read SDK env names of the class;
`S3CredentialDef.from_aws_env(name, ...)` takes the shell's own `AWS_*`, said so explicitly. A
callable returns `{field: value}`, or None for the next source. A non-secret field can be a literal
(`region="eu-west-1"`), which a source overrides. `"1password:NAME"` is planned, refused now. A
source that misses a required field, an unknown field or env name, or a credential scoped to a
storage of another scheme is a scan error. `values=` gives a test that holds it plain `{NAME}`s, such
as the signal an existing file checks with `require-env`.

**Binding.** `scopes` lists storages, each its name or the StorageDef, or `(storage, "ro")`. A
named credential wins; else the one scoped to the storage with the mode the test needs, `ro` for a
storage it only reads (1.9); several need one `default=True`; a storage it writes whose credentials
are all scoped `ro` is a scan error. A test without a credential is a definition of its
own that binds no storage in the scope. A test that holds a credential that does not resolve is
BLOCKED, never SKIPPED. A process has one set of each SDK's names, so a test holds one credential of a
type, and a booted engine (Spark) one for the whole run; several are a scan error: not supported yet.

**Delivery.** The fields reach every engine process of the test as SDK env: a Python body's DuckDB
shell, the `unittest` binary (and by name in `env_passthrough`), and the Spark JVM when it boots. That
env is part of the engine's identity, counted by credential and source, never by value. Every engine
process starts from the shell's env without the SDK prefixes of every credential type (`AWS_`;
`AZURE_`, `IDENTITY_`, `MSI_`, `IMDS_`); `Project(inherit_env=["AWS_CA_BUNDLE"])` keeps names, never a
key, such as `AZURE_CONFIG_DIR` for an az cli login or `AZURE_LOG_LEVEL`.

**Secrets.** For each credential and the test's storages in its scope, DuckDB gets one secret,
`ducktest_<credential name>`: the storage's connection options and scope, and its keys, which no SQL
text, template or init file ever holds. An S3 secret is `PROVIDER credential_chain, CHAIN 'env'`: the
AWS SDK chain reads the keys from the process env, as a DuckDB user's secret does, and the secret
keeps what it found, since an extension may read the keys from the secret alone (delta's scan does).
`REGION` stays named by its env variable where the credential has one: an explicit one wins over the
chain's. `credential_chain` is the aws extension's, so such a secret needs `aws` with `httpfs`: a
`.test` init file requires both, a skip that names the credential where the build has no aws, and a
DuckDB shell autoloads them as a user's does (`LOAD` fails where the build keeps an extension in its
repository), with a setup error that names the credential. A repo whose build has no aws (httpfs's
own) gives the credential `provider="config"`, whose secret names each key by its env variable:
`KEY_ID getenv('AWS_ACCESS_KEY_ID')` in the shell, `'{AWS_ACCESS_KEY_ID}'` in an init file, which the
binary fills. The rule: **bind the storage** (any of `storage=`, `data=`, `temp=`), and
the run sets DuckDB up for it (the scoped secret; `{TEMP_DIR}` there with `temp=`); **bind only the credential**,
and the test gets the keys in its process env and nothing else, and sets DuckDB up itself. No secret
is made where no storage of the test is in the scope; the env is delivered all the same. An Azure credential is a scoped `TYPE azure` secret for every provider, a connection string
(an `AzureServiceDef`'s, or provider `config`) included: `CONNECTION_STRING
getenv('AZURE_STORAGE_CONNECTION_STRING')`, never the global setting
`azure_storage_connection_string`, which a `current_setting()` would read back; an `access_token` is in it by reference
(`AZURE_ACCESS_TOKEN`, ducktest's name: the Azure SDK reads no bearer token from env). A test that
binds the storage but makes its own secret is not supported yet; there is no flag for it. Spark gets, per scoped bucket,
the env credentials provider and the bucket's endpoint, never a key. A body reads what it holds with
`ctx.credential(name)` and `ctx.credentials()`: name, type, `secret_name`, storages, scope, env names,
fields masked, source. A service can make a credential: `LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])`, and
`provider=` passes through to it.

## 2.6 Services

A service runs; a test reaches it through a storage on it (`StorageDef("served", "{local_http.url}",
inputs=[LOCAL_HTTP])`), a credential it makes, or `service=`. A service class names the protocol and
the role, and its `flavor` is what backs it; the tests do not see the flavor. Kinds: `HttpServerDef`,
`S3ServiceDef` (a server that speaks S3; `S3ServiceDef.rustfs("local_s3")` is a local RustFS, the
only flavor built; `LOCAL_S3.storage("local_lake", path="lake")` is an `S3StorageDef` at
`s3://ducktest/lake` on it), `AzureServiceDef` (a server that speaks Azure blob;
`AzureServiceDef.azurite("local_az")` is a local Azurite; `LOCAL_AZ.storage("local_blobs",
path="lake")` is an `AzureStorageDef` at `az://ducktest/lake`, and `LOCAL_AZ.credential(...)` an
`AzureCredentialDef` of its connection string), and the engines. A flavor that is only ever found
running at its endpoint also fits this shape; whether and how cloud flavors come is open.
`storage()` takes the name by position and the rest by keyword: `bucket=` (or `container=`) and
`path=`. The run starts each needed service once, before any test, and stops it after the last.
`.as_existing(address=...)` or `--existing-service NAME=URL` is the same service found running: adopted
when it answers, never removed. A service whose kind knows its address before it starts (S3) is
adopted the same way, with no flag, when it already answers there (mode `found running`). The run
reaches services at `DUCKTEST_SERVICE_HOST` and publishes the ones it starts at
`DUCKTEST_SERVICE_BIND` (both `127.0.0.1` by default; HOST follows BIND): the machine with docker sets
BIND, a machine that only reaches the services sets HOST. A service that does not come up makes its
tests BLOCKED.

**Buckets belong to the service.** `S3ServiceDef.rustfs(buckets=[bucket("data", seed=[(dir, "path")]),
bucket("open", public=True)])` declares more buckets beside the server's own (`ducktest`); they are
made when the server is, before any test, so a test that reaches the server (a storage on it, a
credential it makes) has them. `LOCAL_S3.storage("in_data", bucket="data", path="lake")` is a storage in one; a seed
copies each local directory to its path in the bucket. The
bucket calls are the service's own: create, head, remove (with what is in it), a public-read policy,
as signed S3 API requests. Per bucket: one the run made is removed at the end unless its `retain`
(default `never`) keeps it, or the server was found running (a bucket has a fixed name, so other runs
on that server may use it: it stays); one already there that holds what it seeds (rclone `check`), public when
asked, is used as it is and never changed; one there with other content (or that cannot be made) is
refused, and blocks only the tests that need it: a test needs the bucket of each storage on the server
it binds, and every bucket when it binds none (only the credential) or the server itself. `03` lists
what was refused.
`AzureServiceDef.azurite(containers=[container("data", seed=[(dir, "")], public="blob")])` is the same for
Azure, a container per bucket, with the same rules per container (one module holds them for both), and
Blob service calls signed with the account key (SharedKey); `public` is `True` or
`"container"` (list and read), or `"blob"` (read). Azurite serves blob only: a storage on it other than
`az://`, such as `abfss://`, is a scan error, as it has no DFS endpoint.

**A storage is a URL and a credential.** Its files -- list, remove a path, copy and sync a local
directory in, check one -- go through rclone, whose verbs are the same for S3, Azure blob, GCS and R2;
the store's access object gives rclone its backend. A seed is copied in with it. A run that needs one
of these and finds no rclone on PATH says to install it.

## 2.7 Phases, identity, batching

`scan` (`01-tests.yaml`) -> `plan` (`02-plan.yaml`) -> `provision` (`03`) -> `exec` (`04-events.jsonl`)
-> `cleanup` -> `report`; `run` does all. Identity is by content: `sha(kind, properties, input
identities, mode)`; a child is `sha(parent, key)`; the name is only a label. A test's identity is its
engine instances plus its test environment. `.test` files of one identity share one `duck-sqllogic` batch (an inline
`require` is body, not identity). A Python body is a batch of one; two of one identity may reuse a
warm engine.

## 2.8 What the repo owns

`ducktest_conf.py`: the root's `Project(...)` and the `Rules(...)` of the files below it (1.9); kinds
of its own beside its tests (a catalog service, `UCTable`). ducktest owns lifetime, identity,
delivery, dialects, and the shared kinds.
The name is exact: a `ducktest_config.py`, `ducktestconf.py` or `Ducktest_Conf.py` is not read,
and the run says so in a warning.

The repo's helper code goes on `sys.path` for the whole process, in every phase, so a body that
imports a helper as it runs finds it too. The base is `[tool.ducktest] pythonpath = [...]` in the
repo's `pyproject.toml` (the file `ducktest init` writes), relative to it, read before any conf runs,
so a conf's own imports can use it. `Project(pythonpath=[...])` adds more, relative to the root conf
file; an addition holds from that point on, for the imports below it in the file and for every body.
Every path is inside the repository. Engine processes do not get them: `duckdb` and `unittest` are
not Python, the Spark driver runs in this process, and their process environment is part of their
identity. A body that starts a Python process passes the path itself. `scan --env` prints them
first, each with where it came from.

## 2.9 Words that mean something else in DuckDB or Spark

| Word | Here | Elsewhere |
| --- | --- | --- |
| scope | a credential's storages; nothing else | DuckDB `CREATE SECRET ... SCOPE`: a path prefix |
| env | only an engine's process variables (`process_env`, a credential's SDK names); a test's `{NAME}`s are `ctx.values`, never secrets | `require-env`: a `.test` reads `{NAME}` from the process |
| client | one engine's lease for one test | a DuckDB language binding |
| bind | give a test a storage, credential or service | a prepared-statement parameter; DuckDB's binder |
| parameter | one dimension of a matrix (1.7) | a prepared-statement parameter |

## 2.10 Not built yet

`TableDef` and tables per test; catalog services (UC, Databricks); input slots declared by a kind;
the `1password:` source; credential types other than S3 and Azure (GCS, R2); Spark with an Azure
credential; several credentials of one type in one test; scan checks of plain `{NAME}` references
and of `ctx.client("...")` names; one project that includes another's conf and tests, and the
qualified name that refers across projects; `.test` header blocks as rules; the `Run` object; the events of runs loaded into
a DuckDB file, so a history is a query.

One runner for `.test` files, `duck-sqllogic`: today the `unittest` binary. Convergence with DuckDB's own `test/run` -- its
coverage, its minimal-token output, its options -- is phase 2: needed, not dropped.

---

# Appendix A. The ladder

`examples/ladder/` in this repo; goldens in `tests/ducktest_goldens/ladder/`.

| Rung | Adds | Path |
| --- | --- | --- |
| 00 | an existing repo's `test/` tree: select by path, `--suite default`, SKIPPED | `00_existing/` |
| 01 | no config: a `.test`, a Python body, and a body paired with a `.test` | `01_solo/` |
| 02 | storages, `temp=`, an engine, `default`/`match`, `@test(storage=, temp=)`, a matrix, `ctx.skip` | `02_rules/` |
| 03 | an S3 credential from env; a test that holds none | `03_credentials/` |
| 04 | an HTTP service under a storage, its `{DATA_DIR}` by `data=`; BLOCKED | `04_service/` |
| 05 | a local S3 server (RustFS), a storage and a credential on it | `05_s3/` |
| 06 | a local Azure blob server (Azurite), a storage and a credential on it | `06_azurite/` |
| 07 | one `.test` file on a storage matrix: a local and an S3 cell; BLOCKED | `07_storage_matrix/` |

# Appendix B. The delta cross-engine tests

`test/cross_engine/` in duckdb-delta, branch `test/cross-engine-resources`: six files, DuckDB and
Spark on one Delta table, each test on local storage and on S3 (`[storage=local]`, `[storage=s3]`).
`test/ducktest_conf.py`, in the form of this release:

```python
import os

from ducktest import DuckEngineDef, Project, S3ServiceDef, SparkEngineDef, bucket, default_storage, match

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.abspath(os.environ.get("BUILD_DIR") or os.path.join(REPO, "build", "debug"))
DAT = os.path.join(BUILD, "rust/src/delta_kernel/acceptance/tests/dat/out/reader_tests/generated")

# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
# Its buckets hold the DAT tables the s3_local files read, as scripts/s3/populate.sh fills them in CI.
LOCAL_S3 = S3ServiceDef.rustfs(
    "local_s3",
    buckets=[
        bucket("test-bucket", seed=[(DAT, "dat")]),
        bucket("test-bucket-public", seed=[(DAT, "dat")], public=True),
    ],
)

# The default storage: {DATA_DIR} is <repo>/data, {TEMP_DIR} the test's own place in this run.
LOCAL_DEFAULT = default_storage()
LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="delta")
LOCAL_KEYS = LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])

project = Project(
    # The only DuckDB engine is the default one, so the .test files run on it too.
    engines=[DuckEngineDef("duck", requires=["delta", "json"]), SparkEngineDef.delta(s3a=True)],
    # Each value binds the storage the test is about and writes under it.
    matrix={
        "storage": {
            "local": {"storage": LOCAL_DEFAULT, "temp": LOCAL_DEFAULT},
            "s3": {"storage": LOCAL_LAKE, "temp": LOCAL_LAKE},
        }
    },
    rules=[
        # The .test files stay on local storage.
        match("cross_engine/**", matrix=["storage"]),
        # They bind only the credential and make their own secrets, without URL_STYLE: the server is
        # reached by address, so path style.
        match(
            "sql/cloud/s3_local/**",
            credential=LOCAL_KEYS,
            requires=["httpfs"],
            settings={"s3_url_style": "path"},
            values={"DUCKDB_S3_TEST_SERVER_AVAILABLE": "1", "AWS_ENDPOINT": "{local_s3.host_port}"},
        ),
        # Its tables go under {TEMP_DIR} on the S3 server, so a second run starts from nothing.
        match("sql/cloud/s3_local/create_table_s3.test", storage=LOCAL_LAKE, temp=LOCAL_LAKE),
    ],
)
```

A parameter of the test's own and a keyword value, from `test/cross_engine/create_table.py`, shortened:

```python
@test(engines=["duck", "spark"], matrix=[{"properties": PROPERTY_SETS}])
def duckdb_creates_then_spark_and_duckdb_interleave(ctx):
    duck = ctx.client("duck")
    duck.setup("ATTACH '{TEMP_DIR}/t' AS t (TYPE delta)")
    duck.oks(
        """
        CREATE TABLE t.t (i INTEGER, s VARCHAR) {with_properties};
        INSERT INTO t.t VALUES (1, 'duck'), (2, 'duck'), (3, 'duck');
        """,
        with_properties=with_clause(ctx.params.properties),
    )
```

# Appendix C. Next

- **Azure**: the azure extension's 10 local `.test` files run on `AzureServiceDef.azurite()` unchanged (2026-10-16);
  next, one storage family on three cells, Azurite blob, Azure blob and Azure DFS (`BRIEF-azure.md`
  in the maintainers' notes).
- **httpfs**: a `storage` parameter (http, s3, s3 through a proxy) and an `httpfs_client` parameter (curl,
  httplib; `examples/httpfs/`). Needs a proxy kind.
- **Unity Catalog**: deployments as a parameter binding a catalog service and a storage. Needs
  `TableDef`, catalog services and UC CREATE.

# Appendix D. Open decisions

Settled 2026-09-30: pairing (`ctx.run_paired()`) stays, as a basic way to use the runner; the
binary's `--temp-dir-root` keeps its name (the root is where the temp dirs go) and already takes an
`s3://` root (rung 05).

1. TODOs worth a look: S3 is proven live (rung 05, delta `storage=s3`), but the goldens stay BLOCKED; batch dirs of skipped tests stay
   after a green run; scan does not check `ctx.client("...")` names against `engines=`; delta takes the
   runner from a local path (`pyproject.toml`), by decision until a branch is pushed.

# Appendix E. History

The dated log of what changed in this design, from its first version on 2026-09-25, is
`DESIGN-HISTORY.md` in the maintainers' notes.
