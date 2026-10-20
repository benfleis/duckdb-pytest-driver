# The ladder

Each rung is the test directory of a repo that uses ducktest, and each adds one idea to the one
before. Rung 00 is a repo that has never heard of ducktest. Run these from inside a rung, e.g.
`cd examples/ladder/02_rules`:

    ducktest scan --env                what each test is given, and where each part came from
    ducktest select --explain          what a bare run takes, which rule gave each test what, and why not
    ducktest run                       run it

`run` needs a DuckDB build: set `BUILD_DIR` to the directory that holds `duckdb` and `test/unittest`.
ducktest runs the `.test` files it finds on disk, whether the build lists them or not.

Rung 02 on has a `ducktest_conf.py`: one `project = Project(...)` with the rules that name what the
tests need (`help(Project)` lists the fields; design.md, 1.9). What a rule, a matrix value or a
body's `@test(...)` names is declared, with what it pulls: a storage its server and the credentials
scoped to it. So no rung lists its storages, credentials or services; `engines=` is listed, as an
engine is not reached that way. Each imports what it uses from `ducktest` in one
line. Its variables are UPPER_CASE and its declared names lowercase, each saying the domain and the
role: `LOCAL_S3 = S3ServiceDef.rustfs("local_s3")`, `LOCAL_LAKE`, `"local_lake"`. A file below it
would hold only rules, `rules = Rules(...)`. The scan checks the names the file declares and refers
to; `--strict` also fails on one that nothing reaches.

## 00_existing

A repo as DuckDB core or an extension has it: `.test` files under `test/`, no `ducktest_conf.py`,
nothing written for ducktest. Run it in place, from the top of the repo. When there is a `test/`
directory, ducktest takes it as the root of the tests, so a test id is relative to `test/`
(`sql/math/add.test::add`), but a path you give is relative to where you are:

    ducktest select --explain test/sql/math --suite default
    ducktest run test/sql/math --suite default

A path selects every test under it, the `.test_slow` files too. A bare run leaves those out, because
they are not in the suite `default`; with a path, add `--suite default` to leave them out again.
`many_rows.test_slow` is the slow test here, and `strings/concat.test` is outside the path.

A test is SKIPPED when a `require` in it is not met: here, `require-env DUCKTEST_LADDER_NEVER_SET`
in the middle of `mid_file_skip.test`. The statements above it ran and passed, but ducktest reports
the whole file as SKIPPED; DuckDB's `test/run` counts such a file as passed. Under "skipped", the
report says what each skipped test needed. A Python body skips the same way with `ctx.skip(reason)`.
A repo that gates files this way can give them what they need from that line when it starts a
`ducktest_conf.py`: `match("**", requires_env="DUCKTEST_LADDER_NEVER_SET", values={...})` matches
`mid_file_skip.test` (deprecated, a way in: move to tags or paths).

`{DATA_DIR}` is `data/` at the top of the git repository, and `{TEMP_DIR}` is the test's own place
in the run directory, `ducktest_temp/<run>/` in the working directory. For duckdb core,
that is all: build it, go to the top of the checkout, and run
`ducktest run test/sql/aggregate --suite default`. The build is found in `build/debug/` there.

## 01_solo

A Python body and a `.test` file, and no `ducktest_conf.py` in this directory or above it. Both run
on the built-in DuckDB engine, `duck`: the `.test` file as `duck-sqllogic` (today a `unittest` process),
the body as `duck-shell`, through `ctx.client("duck")`, a Client on that engine's shell. Each runs one query. A body that
needs a second connection, such as a thread that must run beside the first, asks for
`ctx.client("duck", new=True)`: a shell of its own, with the same preamble, closed when the test
ends.

`paired_solo.py` calls `ctx.run_paired()`, which runs `paired_solo.test`, the file of its stem beside
it, through the binary with the body's own test environment: its `{TEMP_DIR}` is the body's place.
A `.test` file with a body of its stem is not a test of its own; only the body runs it. Rungs 02 and 03 pair a body the same way.

## 02_rules

`ducktest_conf.py` declares two local storages, each a `LocalStorageDef`, and a DuckDB engine,
`duck`, that loads `parquet`, listed in the engines of `project = Project(...)`. Each is bound to a
variable, and the rules refer to it by that object; what they name is what the project declares:
`default(storage=SCRATCH_A, temp=SCRATCH_A, engines=[DUCK])` binds that storage for every test and
names `duck` in `engines=`, so every test drives that engine, through a Client made for it.
`storage=` says what a test is about; `temp=` is where it writes, so its `{TEMP_DIR}` is under
`scratch_a`. Without `temp=` it would stay in the run directory: binding a storage does not move it.
`data=` moves `{DATA_DIR}` the same way (rung 04). A reference takes the name too
(`storage="scratch_a"`), as a body's `@test(...)` does; the object gives an editor's
go-to-definition, and the run sees the name either way. Each `match()` rule names one file.
`own_inputs.py` and `paired_own_inputs.py` bind `scratch_b` with `@test(storage=..., temp=...)`, and
that wins over the rule; `select --explain` shows both bindings. `paired_own_inputs.py` calls
`ctx.run_paired()`, which runs `paired_own_inputs.test`, the file of its stem beside it, through the
binary with the body's own test environment, so the file sees `scratch_b` too. `default.test` is
named by no `match()` and runs with what `default()` gives. `rule_matched_body.py` does what
`default.test` does, through the Client on `duck`: its SQL names `'{TEMP_DIR}/t'` as a `.test` file
would, and the Client fills in the test's own place under `scratch_a`. The rule of `excluded.test`
tags it `opt-in`, so the default suite, and a bare run, leave it out, and `select --explain` says
which tag did; `--suite all` runs it.

`matrix_body.py` runs once per cell of its matrix. The project's `matrix=` declares the parameter
`storage`, with the values `a` and `b`; each is a dict of slots, `{"storage": SCRATCH_A, "temp":
SCRATCH_A}`, so a cell binds `scratch_a` or `scratch_b` in both, and replaces what `default()` binds. A
cell is one more rule, applied last: a value that bound `storage=` alone would leave `{TEMP_DIR}` where
`default()` put it. The rule
of `matrix_body.py` names `storage` with `matrix=["storage"]`, and the body defines a parameter of
its own, `case`, with `@test(matrix=[{"case": CASES}])`: 2 values times 2, four cells, such as
`matrix_body.py::matrix_body[storage=a,case=small]`. The fields of a `case` value are template
values (`{a}`, `{b}`), `ctx.params.case` is the whole value, and `{storage}` is the name of the
cell's `storage` value. `--select storage=b` or `--select case=small` takes some of them.

`skipped_body.py` checks for a tool that is not installed and calls `ctx.skip(reason)`: it ends
SKIPPED, and the report lists the reason under "skipped". A body skips only on a condition it checks
itself; an input that does not come up makes the test BLOCKED (rung 04).

A run writes its records to `ducktest_temp/<run>/` in the working directory, and what its
tests write under `<storage>/temp/<run>/` of the storage `temp=` binds (`STORAGE.at("sub")` makes it
`<storage>/sub/<run>/`); when the run ends, that path goes, but the places of the tests that failed.

The batches in `02-plan.yaml`, in the run directory, are labelled by the identity of their tests,
beside the process kind that runs each. The two `.test` files run in one `duck-sqllogic` batch,
although `rule_matched.test` has an inline `require json`: that is part of its body. A Python body
leases the `duck` engine as `duck-shell`, not a batch, so its label is never that of a `.test` batch. `own_inputs.py` and `paired_own_inputs.py`
share one label; their binding on `@test` gives them a label other than that of
`rule_matched_body.py`. Each cell of `matrix_body.py` has a label of its own: its values are part
of its identity.

## 03_credentials

The storage is now an S3 bucket that nothing reads, an `S3StorageDef`. `S3CredentialDef("fake_key", ...)` is scoped to it,
`scopes=[FAKE_S3]`, and takes its two fields, `key_id` and `secret`, from `LADDER_FAKE_KEY_ID` and `LADDER_FAKE_SECRET`;
set both before a run. The rule binds the bucket for the `scoped_*` tests with `storage=`, so they
hold the credential; their `{TEMP_DIR}` stays in the run directory, as nothing binds `temp=`.
`no_secret.test` binds nothing, holds none, and finds no secret. A test without a credential is a
definition of its own, not a cell of one with it.

The keys reach every engine process of a test that holds the credential as `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`: the body's DuckDB shell and the `unittest` binary. `AWS_*` of your own shell
never do. DuckDB gets one secret, `ducktest_fake_key`, scoped to the bucket, whose AWS SDK chain reads
the keys from that env (`PROVIDER credential_chain, CHAIN 'env'`), as a DuckDB user's secret does. The
chain is the aws extension's, so the rung needs a build with `aws` beside `httpfs`; on one without,
the `.test` file is skipped and the bodies error, each saying the credential needs aws. `scan --env`
shows the secret, and the keys masked with where they came from. The init file and the run's own
records hold no key, not even its name.

`scoped_secret_body.py` reads what it holds with `ctx.credentials()` and checks the secret DuckDB
has against it; `paired_scoped_secret.py` does the same with `ctx.credential("fake_key")`, then runs
the `.test` file beside it with `ctx.run_paired()`, which checks again.

## 04_service

The storage is now served over HTTP by a service the run starts: `HttpServerDef` serves a copy of
`data/` from a thread, on a free port, and the storage `served` takes it as an input, with its root
`{local_http.url}`. The rule binds it with `data=` too: a test whose `data=` binds nothing reads the
default, `data/` at the top of the repository; here `{DATA_DIR}`, in `served.test` and in
the SQL of `served_body.py`, is that server's address, and both read `cities.csv` from it. Before a run the address is not known:
`scan --env` shows the root as `<from service local_http>`. The run starts the server before any test and
stops it after the last one.

`blocked.test` binds a second storage in both slots, whose server listens on an address no machine has, so it
cannot start. The test ends BLOCKED with the reason, the rest of the run goes on, and the run is not
green.

## 05_s3

The storage is now a path in a bucket of a server that speaks S3, `S3ServiceDef.rustfs("local_s3")`:
a local RustFS, which the run starts in docker. `S3ServiceDef` names the protocol and the role; its
`flavor` names what backs it, `rustfs` (the only one now), and the tests do not say it.
`LOCAL_S3.storage("local_lake", path="lake")` is an `S3StorageDef` at `s3://ducktest/lake` on it,
and `LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])` is an `S3CredentialDef` of the server's
keys, from `DUCKTEST_S3_LOCAL_ACCESS_KEY` and `DUCKTEST_S3_LOCAL_SECRET_KEY` (`admin` and `password`
when unset). The rule binds `storage=LOCAL_LAKE, temp=LOCAL_LAKE`. Both bodies find one secret,
`ducktest_local_keys`, scoped to the storage (the Python body checks it against
`ctx.credential("local_keys")`), and write and read `{TEMP_DIR}/answer.csv`: a Python body under
`s3://ducktest/lake/temp/<run>/<key>`, a `.test` file under the run's path, where the binary makes
its own place. When the run ends, that path goes, but the places of the tests that failed.

With no S3 server to reach, and no docker to start one, both tests end BLOCKED; that is the golden,
where the self-tests name a server at an address nothing answers.

Before the run starts a server, it looks at the address where it would reach one. A server that
answers there is used as it is: the run never stops it, and a bucket the run makes there stays.
The run reaches the server at
`DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT` (`127.0.0.1:9000` when unset). A server the run starts
in docker is published at `DUCKTEST_SERVICE_BIND:DUCKTEST_S3_LOCAL_PORT`, and when
`DUCKTEST_SERVICE_HOST` is not set, the run reaches it there too.

So when docker runs on one machine and the tests on another, as in a container, set one variable on
each side. Where docker runs, set `DUCKTEST_SERVICE_BIND` to the address the other side reaches this
machine at, e.g. `172.18.0.1`, and start the server with `ducktest provision` or a run. Where the
tests run, set `DUCKTEST_SERVICE_HOST` to that same address. Set `DUCKTEST_S3_LOCAL_PORT` on both sides if
the port is not 9000.

`--existing-service local_s3=http://HOST:PORT` still names a server at any address, but a server that
answers where the run reaches it needs no flag.

## 06_azurite

Rung 05 again, on Azure blob storage: `AzureServiceDef.azurite("local_az")` is a local Azure blob
server, Azurite (`flavor="azurite"`, the only one), started in docker or found running.
`LOCAL_AZ.storage("local_blobs", path="lake")` is an `AzureStorageDef` at `az://ducktest/lake` on it,
and `LOCAL_AZ.credential("local_keys", scopes=[LOCAL_BLOBS])` is an
`AzureCredentialDef` of the server's connection string, made of Azurite's own account,
`devstoreaccount1`, and its well-known key (`DUCKTEST_AZ_LOCAL_ACCOUNT` and `DUCKTEST_AZ_LOCAL_KEY`
give another). The connection string reaches every engine process of the test as
`AZURE_STORAGE_CONNECTION_STRING`; DuckDB gets a `TYPE azure` secret scoped to the storage, which
names it (`getenv(...)` in the shell, `'{...}'` in a `.test` init file) and never holds the value.
The rule binds `temp=LOCAL_BLOBS` beside `storage=LOCAL_BLOBS`, so both tests write and read `{TEMP_DIR}/answer.csv`
under it, and the run removes what they wrote with rclone's `azureblob` backend.

With no Azurite to reach, and no docker to start one, both tests end BLOCKED; that is the golden.
The run reaches Azurite at `DUCKTEST_SERVICE_HOST:DUCKTEST_AZ_LOCAL_PORT` (`127.0.0.1:10000` when
unset) and publishes one it starts at `DUCKTEST_SERVICE_BIND`, as for S3.

Azurite serves blob only. A storage on it is `az://`; an `abfss://` storage on it is a scan error,
as it has no DFS endpoint.

## 07_storage_matrix

One `.test` file, `write_read.test`, on both cells of a storage matrix: it writes
`{TEMP_DIR}/answer.csv` and reads it back, and says nothing of where. The project's `matrix=` gives
the parameter `storage` two values, `local`, the storage `local_dir`, a directory under the rung, and
`s3`, the storage `local_lake` on a local S3 server as in rung 05; each binds `storage=` and `temp=`, so `{TEMP_DIR}` is on that
storage. `default(matrix=["storage"])` puts every test on the matrix, so the file runs twice:
`write_read.test::write_read[storage=local]` and `write_read.test::write_read[storage=s3]`. The
`s3` value also has `requires=["httpfs"]`, which only that cell loads, and it holds the credential
`local_keys`, as its storage is in the credential's scope; the `local` cell holds none. The two cells run in
batches of their own, as their bindings differ.

With no S3 server to reach, and no docker to start one, the `s3` cell ends BLOCKED and the `local`
cell passes; that is the golden. To make both green, start docker where the run can use it, or set
`DUCKTEST_SERVICE_HOST` (and `DUCKTEST_S3_LOCAL_PORT`) to where an S3 server answers, as in rung 05.
`--select storage=local` runs the one cell only.
