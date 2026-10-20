# ducktest

DuckDB and its extensions are tested mostly with `.test` files: SQL statements and their expected
results, in the SQLLogic format. ducktest runs these files through DuckDB's own `unittest` binary,
and it runs Python test bodies for what SQL alone cannot set up: a second engine, a server, a
credential. Your `.test` files stay as they are. When the tests need more, one file,
`ducktest_conf.py`, declares it; ducktest makes it, gives it to the tests as `{NAME}` values, and
removes it after the run. It does not use pytest.

## Install

The package is `duckdb-ducktest`, and it gives the command `ducktest`. It needs DuckDB v2.0
or newer: the build's `duckdb` and `test/unittest`. An older build is refused at start, with the
version it found.

ducktest is not on the remote yet: its branch is not pushed. Until it is, install from a local
checkout of this repo. Editable, so that your changes here take effect at once:

    uv tool install --editable /path/to/duckdb-pytest-driver

That is enough for a repo with only `.test` files, such as duckdb core.

For a repo with Python test bodies, give the repo a test environment with `ducktest init`, run
once at the top of its checkout:

    ducktest init --runner path:/path/to/duckdb-pytest-driver --dep pyspark==4.2.0
    uv sync && .venv/bin/ducktest run

It writes `pyproject.toml`: the runner (`path:DIR` for a local checkout, editable; `git+URL@REV`
once the branch is pushed; the default is where this `ducktest` was installed from), its extras
(`--extra`), the packages the bodies import (`--dep`), Python `>=3.12`, and `package = false`. Then
it runs `uv lock`; check `uv.lock` in. `--conf [DIR]` also writes a starter `ducktest_conf.py` in
`test/` or DIR; a tree of plain `.test` files needs none. A file that is already there and different
is not overwritten: init prints the diff and exits 1; merge it by hand, or run again with `--write`.
It also makes sure `.gitignore` ignores the runner's default temp root (`ducktest_temp/`), creating
or appending to it as needed.

## A first run, in duckdb core

Build duckdb (`make debug`), then run from the top of the checkout:

    ducktest run test/sql/aggregate --suite default

You need no config file. ducktest finds the tests in `test/` and the build in `build/debug/`. For
another build, use `--build release`, or set `BUILD_DIR` to the directory that holds `duckdb` and
`test/unittest`.

- **Select by path.** A path selects every test under it, also the `.test_slow` files.
  `--suite default` leaves the slow files out, as a bare `ducktest run` does. Without it, the
  example above takes more than ten minutes.
- **Test ids.** A test id is relative to `test/`, for example
  `sql/aggregate/qualify/test_qualify.test::test_qualify`. A path is relative to where you are, so
  it starts with `test/`. `--id <test id>` runs one test. A test that runs once per cell of a
  matrix has one id for each, with the value of every parameter: `create.py::t[storage=s3,case=varchar]`;
  `--select storage=s3` takes the cells with that value.
- **Results.** Each test gets one line: PASSED, FAILED, ERROR, SKIPPED or BLOCKED. A failure
  prints what is different, what was kept, and the command that runs it again.
  - **ERROR**: the test started, and the harness or an engine broke under it (a setup step failed,
    a shell died).
  - **BLOCKED**: the test never started, because an input it binds did not resolve: a service that
    did not come up, a credential with no source, a storage that was refused.
- **Exit code.** 0 only when no test FAILED, ERRORed or was BLOCKED, and at least one PASSED. A run
  where every test is SKIPPED is red: it tested nothing.
- **SKIPPED** means that a `require` in the file was not met: an extension that is not built, or an
  environment variable that is not set. The whole file is SKIPPED, also when statements above the
  `require` ran. DuckDB's `test/run` counts such a file as passed, so the counts can be different.
  A Python body skips with `ctx.skip(reason)`, or `ducktest.skip(reason)` from a helper.
- **Speed.** `--workers N` (default 4) runs N batches at the same time. A batch is one `unittest`
  process with up to 10 `.test` files. For a big run, `--level terse` prints only the tests that did not pass, and the summary.
- **Diagnostics.** `-v` and `-vv` print ducktest's own diagnostics on stderr, at INFO and DEBUG.

Each run writes a run directory, `ducktest_temp/<run>/`, in the working directory. The
last line of the output gives its path. It holds `01-tests.yaml` (the tests and what each gets),
`02-plan.yaml` (the batches), `03-provision.yaml` (what was made), `04-events.jsonl` (what
happened), `05-report.json` and `05-report.yaml` (the results), and `ducktest.log` (diagnostics at
DEBUG). `ducktest report --run-dir <run dir>` prints the results again; `--junit FILE` also writes
JUnit XML for CI.

To look before you run:

    ducktest select test/sql/aggregate --suite default    # which tests run, and why the others do not
    ducktest scan --env test/sql/aggregate                # what each test gets, and from where

An extension repo works the same way: build it, then run from the top of its checkout. When its
tests need a service, a storage or a credential, write a `ducktest_conf.py`.

## A first `ducktest_conf.py`

The common case: `.test` files that hold `require-env S3_TEST_SERVER_AVAILABLE` and need a local S3
server and its keys. `test/ducktest_conf.py`:

```python
from ducktest import Project, S3ServiceDef, match

LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
LOCAL_KEYS = LOCAL_S3.credential("local_keys", scopes=[])

project = Project(rules=[
    match("**", requires_env="S3_TEST_SERVER_AVAILABLE", credential=LOCAL_KEYS,
          values={"S3_TEST_SERVER_AVAILABLE": "1", "S3_ENDPOINT": "{local_s3.host_port}"}),
])
```

The rule selects the files with that `require-env` line, gives them the keys, and fills the names
they require; `{local_s3.host_port}` is the address of the server. What a rule names is all a conf
declares: the keys bring their server, as a storage brings its server and the keys scoped to it.
`Project(engines=[...])` is the one list you write, for an engine other than the built-in one. `ducktest select --strict --explain`
checks the conf before you run. The ladder adds one idea per rung, and `docs/design.md` has every
field.

A conf imports everything from `ducktest`, in one line. A declaration is a class whose name ends in
`Def`, with its makers on the class (`S3ServiceDef.rustfs(...)`); a function is a verb
(`default(...)`, `match(...)`, `bucket(...)`). The name, the first argument, is what
`ctx.client(...)`, `{local_s3.host_port}` and `--existing-service local_s3=...` use. A maker gives one
when it is left out: `s3` for `S3ServiceDef.rustfs()`, `az` for `AzureServiceDef.azurite()`, `spark`
for `SparkEngineDef.delta()`; the built-in DuckDB engine is `duckdb`.

## Where services run

A local server (`S3ServiceDef.rustfs()`, `AzureServiceDef.azurite()`) is reached at `DUCKTEST_SERVICE_HOST` and its port
(`DUCKTEST_S3_LOCAL_PORT`, default 9000; `DUCKTEST_AZ_LOCAL_PORT`, default 10000). When a server
already answers there, the run uses it and never stops it. Else the run starts one in docker and
publishes it at `DUCKTEST_SERVICE_BIND`. Both addresses default to `127.0.0.1`; with only
`DUCKTEST_SERVICE_BIND` set, the run reaches its servers there too (`0.0.0.0` at `127.0.0.1`).

- Docker and tests on one machine: set nothing.
- Docker on one machine, tests on another (a sandbox, a container): on the machine with docker, set
  `DUCKTEST_SERVICE_BIND` to the address the other side reaches it at, and start the servers with a
  run there, or with the phases `ducktest scan`, `plan` and `provision` on one `--run-dir D`; stop
  them later with `ducktest cleanup --run-dir D`. On the machine that only reaches them, set
  `DUCKTEST_SERVICE_HOST` to that address. Set the port on both sides if it is not the default.
- A server at any other address: `--existing-service NAME=URL`, or
  `DUCKTEST_EXISTING_SERVICE_<NAME>=URL` (`NAME` in upper case). The run adopts it and never stops it.

## In CI

A GitHub Actions job for a repo that `ducktest init` set up:

```yaml
    steps:
      - uses: actions/checkout@v4
        with: { submodules: recursive }
      - uses: astral-sh/setup-uv@v6
      - run: make debug             # or download a DuckDB build and set BUILD_DIR to it
      - run: uv sync
      - run: .venv/bin/ducktest run --strict --junit ducktest.xml
```

`--strict` makes a declaration in a `ducktest_conf.py` that nothing uses an error, not a warning:
right for CI, where nobody reads the warnings. The exit code is red when nothing passed, so a job
whose tests all skip (a missing secret, an unset variable) fails. On a hosted runner, docker and the
tests share the machine, so the servers need no address settings. When the job runs in a
`container:`, its servers run in the runner's docker: set `DUCKTEST_SERVICE_BIND=0.0.0.0`, and
`DUCKTEST_SERVICE_HOST` to the address the container reaches the runner at ("Where services run").

## The phases

`ducktest run` does all phases in one process:

1. **scan**: find the tests and what each needs.
2. **plan**: put the tests into batches.
3. **provision**: start the services and make the storages.
4. **exec**: run the batches.
5. **cleanup**: remove what the passed tests wrote, and stop the services. What it cannot remove
   is in the report, and the run is red.
6. **report**: print the results.

Each phase is also a command of its own (`ducktest scan`, `plan`, `provision`, `exec`,
`cleanup`, `report`), which reads and writes a run directory. Use them to start services in one
place and run the tests in another, or to report a run again.

## Read next

- [examples/ladder/](examples/ladder/README.md): small test trees, from an existing repo with no
  config (rung 00) to a local S3 server (rung 05) and a local Azure blob server (rung 06). Each rung
  adds one idea.
- [docs/design.md](docs/design.md): how to write a test (Part 1), how it works
  (Part 2), how to move an existing repo (1.6), and how to run a test over a matrix (1.7).
- [docs/status.md](docs/status.md): what works today, and what is open.
- `ducktest <command> --help`.

## Develop ducktest

The self-tests run against fake binaries. With `DUCKDB_SHELL` set to a built `duckdb` that has
`test/unittest` beside it, the ladder runs on that build too:

    DUCKDB_SHELL=<a duckdb shell> uv run --all-extras --group dev pytest -q
    uvx ruff check .

