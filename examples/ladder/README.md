# The ladder

Each rung is the test directory of a repo that uses ducktest5, and each adds one idea to the one
before. Run these from inside a rung, e.g. `cd examples/ladder/02_rules`:

    ducktest5 scan --env                what each test is given, and where each part came from
    ducktest5 select --explain          what a bare run takes, which rule gave each test what, and why not
    ducktest5 run --no-binary-check     run it

`run` needs a DuckDB build: set `BUILD_DIR` to the directory that holds `duckdb` and `test/unittest`.
`--no-binary-check` is there because the build does not list these `.test` files.

## 01_solo

One Python body and one `.test` file, and no `ducktest_conf.py` in this directory or above it. Both
run on the built-in DuckDB engine, `duckdb`: the `.test` file in a `unittest` process, the body
through `ctx.client("duckdb")`, a Client on that engine's shell. Each runs one query. A body that
needs a second connection, such as a thread that must run beside the first, asks for
`ctx.client("duckdb", new=True)`: a shell of its own, with the same preamble, closed when the test
ends.

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

The storage is now an S3 bucket that nothing reads. A credential scoped to it takes a fake key from
`LADDER_FAKE_KEY_ID` and `LADDER_FAKE_SECRET`; set both before a run. The axis `auth` makes two tests
of each body: `key` binds the credential, and `none` unsets the names it delivers, which hides it.
Each body asks DuckDB for its secrets: one, scoped to the bucket, under `auth=key`, and none under
`auth=none`; a Python body reads the axis value as `ctx.env.auth`. `paired_scoped_secret.py` asks,
then runs the `.test` file beside it with `ctx.run_paired()`, which asks again. A Python body's DuckDB engine creates the secret with the values. For a `.test` body the
init file holds only a template, `KEY_ID '{AWS_ACCESS_KEY_ID}'`, and the binary fills it from the
variables passed through to it; `scan --env` shows it as the `secret:` line. The init file and the
run's own records hold the names of the secrets, never their values.

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

The storage is now a prefix in a bucket of a local S3-compatible server, `s3_local()`: the run
starts one in docker, and a test does not say what backs it. `S3.storage("lake", "lake")` is
`s3://ducktest/lake` on it, and `S3.credential("keys", scopes=["lake"])` is the server's keys, from
`DUCKTEST_S3_ACCESS_KEY` and `DUCKTEST_S3_SECRET_KEY` (`admin` and `password` when unset). Both
bodies find one secret, scoped to the storage, and write and read `{TEMP_DIR}/answer.csv`: a Python
body under `s3://ducktest/lake/temp/<run>/<key>`, a `.test` file under the run's prefix, where the
binary makes its own place. When the run ends, that prefix goes, but the places of the tests that
failed.

With no S3 server to reach, and no docker to start one, both tests end BLOCKED; that is the golden,
where the self-tests name a server at an address nothing answers.

Before the run starts a server, it looks at the address where it would reach one. A server that
answers there is used as it is: the run makes its bucket, and never stops or removes it. The run
reaches the server at `DUCKTEST_SERVICE_HOST:DUCKTEST_S3_PORT` (`127.0.0.1:9000` when unset). A
server the run starts in docker is published at `DUCKTEST_SERVICE_BIND:DUCKTEST_S3_PORT`, and when
`DUCKTEST_SERVICE_HOST` is not set, the run reaches it there too.

So when docker runs on one machine and the tests on another, as in a container, set one variable on
each side. Where docker runs, set `DUCKTEST_SERVICE_BIND` to the address the other side reaches this
machine at, e.g. `172.18.0.1`, and start the server with `ducktest5 provision` or a run. Where the
tests run, set `DUCKTEST_SERVICE_HOST` to that same address. Set `DUCKTEST_S3_PORT` on both sides if
the port is not 9000.

`--existing-service s3=http://HOST:PORT` still names a server at any address, but a server that
answers where the run reaches it needs no flag.
