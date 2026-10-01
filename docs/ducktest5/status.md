# ducktest5: release candidate 1 (2026-09-30)

## What it is

ducktest5 runs the tests of a DuckDB extension: `.test` files through DuckDB's `unittest` binary,
and Python test bodies that drive DuckDB and Spark. It does not use pytest. One file,
`ducktest_conf.py`, declares the engines, storages, credentials and services. ducktest5 makes what
each test needs, gives it to the test as `{NAME}` values, and removes it after the run.

Code: `src/ducktest5/` in this repo, branch `feat/resources`. It will be squashed onto `dev/v0.5`.

## What works today

| What | Result |
| --- | --- |
| Runner self-tests | 677 passed |
| The ladder, rungs 01-05 | pass; the self-tests compare them with goldens |
| Rung 05 (S3) with a live RustFS server | passes; its golden is the BLOCKED case (no server) |
| duckdb-delta, `storage` axis `local` and `s3`, live RustFS | 118 passed, 1 failed, 44 skipped |

The one delta failure is `sql/cloud/gcs.test`. It needs the network. The 44 skips are `.test`
files that need an environment variable that is not set. Without an S3 server, the `s3` cases are
BLOCKED and the `local` cases pass.

## How to run it

Self-tests, in the runner repo:

    uv run --all-extras --group dev pytest -q tests/test_ducktest5_*.py

The ladder: go into one rung and run the three commands. The runner repo has no DuckDB build, so
set `BUILD_DIR` to a directory that holds `duckdb` and `test/unittest`. Rung 03 also needs
`LADDER_FAKE_KEY_ID` and `LADDER_FAKE_SECRET` (any values).

    cd examples/ladder/02_rules
    ducktest5 scan --env               # what each test gets, and from where
    ducktest5 select --explain         # what a run takes, and why
    ducktest5 run --no-binary-check    # run it

duckdb-delta, branch `test/cross-engine-resources`. Run from the top of the repo; the build is found in
`build/debug/` there (`--build release` for another one).

    uv sync
    .venv/bin/ducktest5 run

S3 for the `s3` cases. ducktest5 reaches an S3 server at `DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT`.
If a server answers there, the run uses it and does not stop it. If not, the run starts RustFS in
docker and publishes it at `DUCKTEST_SERVICE_BIND:DUCKTEST_S3_LOCAL_PORT`.

- Defaults: the port is 9000. With neither address set, both are `127.0.0.1`. When only
  `DUCKTEST_SERVICE_BIND` is set, the run also reaches the server there (`0.0.0.0` is reached at
  `127.0.0.1`).
- Docker and tests on one machine: set nothing.
- Docker on one machine, tests on another (for example, tests in a container):
  - On the machine with docker, set `DUCKTEST_SERVICE_BIND=<addr>`, the address the test side
    reaches this machine at. Start the server with a run there, or with the phases:
    `ducktest5 scan --run-dir D`, `ducktest5 plan --run-dir D`, `ducktest5 provision --run-dir D`.
    Stop it later with `ducktest5 cleanup --run-dir D`.
  - On the test side, set `DUCKTEST_SERVICE_HOST=<addr>`.
  - Set `DUCKTEST_S3_LOCAL_PORT` on both sides if the port is not 9000.
- Other server settings are `DUCKTEST_S3_LOCAL_*`: `ACCESS_KEY`, `SECRET_KEY` (default `admin`,
  `password`), `REGION`, `BUCKET`, `IMAGE`.
- `--existing-service s3=http://HOST:PORT` names a server at any other address.
- Cleanup on S3 uses `rclone` if it is on PATH, else boto3 (the `s3` extra).
- If Spark does not start in a container, try `JAVA_TOOL_OPTIONS=-XX:-UseContainerSupport`.

## Where to read

- `README.md`: install, and a first run in an existing repo.
- `examples/ladder/README.md`: six small test trees. Each adds one idea: an existing repo, no
  config, rules, credentials, an HTTP service, S3.
- [design.md](design.md). Part 1: how to write a test. Part 2:
  how it works, for people who write resource kinds or debug the runner. Appendix B: the delta
  configuration.

## Known open

- The delta `pyproject.toml` takes the runner from a local path. On another machine, change
  `[tool.uv.sources]` before `uv sync`.
- Scan does not check `{NAME}` names or `ctx.client("...")` names. A typo shows only when the
  statement runs.
- After a green run, the batch directory of a `.test` batch with a skipped test stays in the run
  directory.
- Cleanup does not remove files under an s3 storage that is not on the local S3 server.
- After a killed run, cleanup does not sweep a storage or service that takes inputs.
- A `.test` batch under a variant's own temp root is not under the run token, so cleanup cannot
  find it.
- A `ducktest_conf.py` with declarations below the project root is an error, but the error does
  not say which directory to run from.
- A remote storage has no local twin (`LOCAL_TEMP_DIR`, `LOCAL_DATA_DIR`).
- The ladder has no two-engine rung and no example of `setup`, `teardown` or `params`.
- Rung 05's golden is the BLOCKED case. A live S3 run is checked by hand, not by the self-tests.

- Two credentials of one type in one test (wiring, `wired(...)`) are a scan error until wiring is
  built.
- Engines no longer take `AWS_*` from the shell's env. A repo whose tests relied on personal keys
  declares `S3CredentialDef.from_aws_env(...)`, and lists other names it needs (`AWS_CA_BUNDLE`) in
  `INHERIT_ENV`.

## Not in this release, on purpose

- Tables per test (`TableDef`) and catalog services (Unity Catalog, Databricks).
- The `1password:` credential source. It is accepted in a declaration and refused when used.
- Azure (Azurite) and an HTTP proxy kind for httpfs. Credential types other than S3 (Azure, GCS, R2).
- MinIO, and any choice of S3 server. RustFS is the only one; `DUCKTEST_S3_LOCAL_IMAGE` changes
  the image.
- pytest. The old pytest-based `ducktest` stays in the same repo and does not change.
