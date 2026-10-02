# The ladder

Each rung is the test directory of a repo that uses ducktest5, and each adds one idea to the one
before. Rung 00 is a repo that has never heard of ducktest5. Run these from inside a rung, e.g.
`cd examples/ladder/02_rules`:

    ducktest5 scan --env                what each test is given, and where each part came from
    ducktest5 select --explain          what a bare run takes, which rule gave each test what, and why not
    ducktest5 run --no-binary-check     run it

`run` needs a DuckDB build: set `BUILD_DIR` to the directory that holds `duckdb` and `test/unittest`.
`--no-binary-check` is there because the build does not list these `.test` files.

## 00_existing

A repo as DuckDB core or an extension has it: `.test` files under `test/`, no `ducktest_conf.py`,
nothing written for ducktest5. Run it in place, from the top of the repo. When there is a `test/`
directory, ducktest5 takes it as the root of the tests, so a test id is relative to `test/`
(`sql/math/add.test::add`), but a path you give is relative to where you are:

    ducktest5 select --explain test/sql/math --suite default
    ducktest5 run --no-binary-check test/sql/math --suite default

A path selects every test under it, the `.test_slow` files too. A bare run leaves those out, because
they are not in the suite `default`; with a path, add `--suite default` to leave them out again.
`many_rows.test_slow` is the slow test here, and `strings/concat.test` is outside the path.

A test is SKIPPED when a `require` in it is not met: here, `require-env DUCKTEST_LADDER_NEVER_SET`
in the middle of `mid_file_skip.test`. The statements above it ran and passed, but ducktest5 reports
the whole file as SKIPPED; DuckDB's `test/run` counts such a file as passed. Under "skipped", the
report says what each skipped test needed. A Python body skips the same way with `ctx.skip(reason)`.

`{DATA_DIR}` is `data/` at the top of the git repository, and `{TEMP_DIR}` is the test's own place
in the run directory, `duckdb_unittest_tempdir/<run>/` in the working directory. For duckdb core,
that is all: build it, go to the top of the checkout, and run
`ducktest5 run test/sql/aggregate --suite default`. The build is found in `build/debug/` there, and
the binary lists the files, so `--no-binary-check` is not needed.

## 01_solo

A Python body and a `.test` file, and no `ducktest_conf.py` in this directory or above it. Both run
on the built-in DuckDB engine, `duckdb`: the `.test` file in a `unittest` process, the body through
`ctx.client("duckdb")`, a Client on that engine's shell. Each runs one query. A body that
needs a second connection, such as a thread that must run beside the first, asks for
`ctx.client("duckdb", new=True)`: a shell of its own, with the same preamble, closed when the test
ends.

`paired_solo.py` calls `ctx.run_paired()`, which runs `paired_solo.test`, the file of its stem beside
it, through the binary with the body's own test environment: its `{TEMP_DIR}` is the body's place.
A `.test` file with a body of its stem is not a test of its own; only the body runs it. Rungs 02 and 03 pair a body the same way.

## 02_rules

`ducktest_conf.py` declares two local storages and, among its engines, a DuckDB engine, `duck`, that
loads `parquet`. `default()` binds the role `storage` to `scratch_a` for every test and names `duck`
in `engines=`: every test drives that engine, through a Client made for it. Each `match()` rule names
one file. `own_inputs.py` and `paired_own_inputs.py` bind `storage` to `scratch_b` on `@test(...)`,
and that wins over the rule; `select --explain` shows both bindings. `paired_own_inputs.py` calls
`ctx.run_paired()`, which runs `paired_own_inputs.test`, the file of its stem beside it, through the
binary with the body's own test environment, so the file sees `scratch_b` too. `default.test` is named by no
`match()` and runs with what `default()` gives. `rule_matched_body.py` does what `default.test` does,
through the Client on `duck`: its SQL names `'{TEMP_DIR}/t'` as a `.test` file would, and the Client
fills in the test's own place under `scratch_a`. The rule of `excluded.test` declares it out of the
default suite, so a bare run leaves it out and `select --explain` names that rule; `--suite all` runs
it.

`skipped_body.py` checks for a tool that is not installed and calls `ctx.skip(reason)`: it ends
SKIPPED, and the report lists the reason under "skipped". A body skips only on a condition it checks
itself; an input that does not come up makes the test BLOCKED (rung 04).

A run writes its records to `duckdb_unittest_tempdir/<run>/` in the working directory, and what its
tests write under `<storage>/temp/<run>/`; when the run ends, that prefix goes, but the places of the
tests that failed.

The batches in `02-plan.yaml`, in the run directory, are labelled by the identity of their tests.
The two `.test` files run in one `unittest` process, although `rule_matched.test` has an inline
`require json`: that is part of its body. A Python body leases the `duck` engine, not a `unittest`
process, so its label is never that of a `.test` batch. `own_inputs.py` and `paired_own_inputs.py`
share one label; their binding on `@test` gives them a label other than that of
`rule_matched_body.py`.

## 03_credentials

The storage is now an S3 bucket that nothing reads. `S3CredentialDef("fake_key", ...)` is scoped to it
and takes its two fields, `key_id` and `secret`, from `LADDER_FAKE_KEY_ID` and `LADDER_FAKE_SECRET`;
set both before a run. The rule binds the bucket for the `scoped_*` tests, so they hold the
credential; `no_secret.test` binds nothing, holds none, and finds no secret. A test without a
credential is a definition of its own, not a variant of one with it.

The keys reach every engine process of a test that holds the credential as `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`: the body's DuckDB shell and the `unittest` binary. `AWS_*` of your own shell
never do. DuckDB gets one secret, `ducktest_fake_key`, scoped to the bucket, that reads the keys by
name: `getenv('AWS_ACCESS_KEY_ID')` in the shell, `'{AWS_ACCESS_KEY_ID}'` in a `.test` batch's init
file, which the binary fills from its env. `scan --env` shows the secret, and the keys masked with
where they came from. The init file and the run's own records hold the names of the keys, never
their values.

`scoped_secret_body.py` reads what it holds with `ctx.credentials()` and checks the secret DuckDB
has against it; `paired_scoped_secret.py` does the same with `ctx.credential("fake_key")`, then runs
the `.test` file beside it with `ctx.run_paired()`, which checks again.

## 04_service

The storage is now served over HTTP by a service the run starts: `HttpServerDef` serves a copy of
`data/` from a thread, on a free port, and the storage `served` takes it as an input, with its root
and its `data` `{http.url}`. A storage that declares no `data` leaves `{DATA_DIR}` to the default,
`data/` at the top of the repository; this one declares it, so `{DATA_DIR}`, in `served.test` and in
the SQL of `served_body.py`, is that server's address, and both read `cities.csv` from it. Before a run the address is not known:
`scan --env` shows the root as `<from service http>`. The run starts the server before any test and
stops it after the last one.

`blocked.test` binds a second storage, whose server listens on an address no machine has, so it
cannot start. The test ends BLOCKED with the reason, the rest of the run goes on, and the run is not
green.

## 05_s3

The storage is now a prefix in a bucket of a local S3-compatible server, `s3_local(flavor="rustfs")`:
the run starts one in docker. `flavor` names what backs it, RustFS (the default, and the only one
now); the tests do not say it. `S3.storage("lake", "lake")` is
`s3://ducktest/lake` on it, and `S3.credential("keys", scopes=["lake"])` is an `S3CredentialDef` of
the server's keys, from `DUCKTEST_S3_LOCAL_ACCESS_KEY` and `DUCKTEST_S3_LOCAL_SECRET_KEY` (`admin` and
`password` when unset). Both bodies find one secret, `ducktest_keys`, scoped to the storage (the
Python body checks it against `ctx.credential("keys")`), and write and read `{TEMP_DIR}/answer.csv`: a Python
body under `s3://ducktest/lake/temp/<run>/<key>`, a `.test` file under the run's prefix, where the
binary makes its own place. When the run ends, that prefix goes, but the places of the tests that
failed.

With no S3 server to reach, and no docker to start one, both tests end BLOCKED; that is the golden,
where the self-tests name a server at an address nothing answers.

Before the run starts a server, it looks at the address where it would reach one. A server that
answers there is used as it is: the run makes its bucket, and never stops or removes it. The run
reaches the server at `DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT` (`127.0.0.1:9000` when unset). A
server the run starts in docker is published at `DUCKTEST_SERVICE_BIND:DUCKTEST_S3_LOCAL_PORT`, and when
`DUCKTEST_SERVICE_HOST` is not set, the run reaches it there too.

So when docker runs on one machine and the tests on another, as in a container, set one variable on
each side. Where docker runs, set `DUCKTEST_SERVICE_BIND` to the address the other side reaches this
machine at, e.g. `172.18.0.1`, and start the server with `ducktest5 provision` or a run. Where the
tests run, set `DUCKTEST_SERVICE_HOST` to that same address. Set `DUCKTEST_S3_LOCAL_PORT` on both sides if
the port is not 9000.

`--existing-service s3=http://HOST:PORT` still names a server at any address, but a server that
answers where the run reaches it needs no flag.

## 06_azurite

Rung 05 again, on Azure blob storage: `az_local()` is a local Azure blob server, Azurite
(`flavor="azurite"`, the only one), started in docker or found running. `AZ.storage("blobs",
"lake")` is `az://ducktest/lake` on it, and `AZ.credential("keys", scopes=["blobs"])` is an
`AzureCredentialDef` of the server's connection string, made of Azurite's own account,
`devstoreaccount1`, and its well-known key (`DUCKTEST_AZ_LOCAL_ACCOUNT` and `DUCKTEST_AZ_LOCAL_KEY`
give another). The connection string reaches every engine process of the test as
`AZURE_STORAGE_CONNECTION_STRING`; DuckDB gets one secret, `ducktest_keys`, `TYPE azure`, that reads
it by that name and is scoped to the storage. Both tests write and read `{TEMP_DIR}/answer.csv`
under it, and the run removes what they wrote with rclone's `azureblob` backend.

With no Azurite to reach, and no docker to start one, both tests end BLOCKED; that is the golden.
The run reaches Azurite at `DUCKTEST_SERVICE_HOST:DUCKTEST_AZ_LOCAL_PORT` (`127.0.0.1:10000` when
unset) and publishes one it starts at `DUCKTEST_SERVICE_BIND`, as for S3.

Azurite serves blob only. A storage on it is `az://`; an `abfss://` storage on it is a scan error,
as it has no DFS endpoint.
