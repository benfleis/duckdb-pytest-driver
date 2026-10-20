# ducktest: status (2026-10-20)

## What it is

ducktest runs the tests of a DuckDB extension: `.test` files through DuckDB's `unittest` binary,
and Python test bodies that drive DuckDB and Spark. It does not use pytest. One file,
`ducktest_conf.py`, declares the engines, storages, credentials and services, and its rules name
what each test needs; what they reach is declared, nothing else is listed. ducktest makes what
each test needs, gives it to the test as `{NAME}` values, and removes it after the run.

Code: `src/ducktest/` in this repo, branch `dev/v0.5`.

## What works today

| What | Result |
| --- | --- |
| Runner self-tests | 980 passed, 2 skipped (2026-10-20, a v2.0 debug build of duckdb-delta's; no live S3 or Azurite) |
| The declared set | what the rules, the matrix values and the bodies reach, with what a storage or a credential pulls; `Project(engines=)` is the one list a conf writes (design.md, 1.9) |
| A conf's imports | one line, `from ducktest import ...`: classes with `Def` and their makers, and the verbs (design.md, Part 1) |
| `storage=`, `data=`, `temp=` | three slots: `storage=` gives a storage's values, `data=` `{DATA_DIR}`, `temp=` `{TEMP_DIR}`; `STORAGE.at("sub")` a place under one (design.md, 1.1) |
| The ladder, rungs 00-07 | pass; the self-tests compare them with goldens |
| Rung 05 (S3) with a live RustFS server | passes; its golden is the BLOCKED case (no server) |
| Rung 06 (Azure blob) with a live Azurite | passes (2026-10-02); its golden is the BLOCKED case |
| duckdb-delta, `storage` parameter `local` and `s3`, live RustFS | 121 passed, 1 failed, 41 skipped (2026-10-07, on the earlier conf form and runner name; the failure was fixed upstream in #392) |
| duckdb-azure, the 10 local `.test` files on Azurite | pass (2026-10-16, on the earlier conf form and runner name) |

The 41 delta skips are `.test`
files that need an environment variable that is not set. Without an S3 server, the `s3` cases are
BLOCKED and the `local` cases pass.

## How to run it

Self-tests, in the runner repo:

    uv run --all-extras --group dev pytest -q

The ladder: go into one rung and run the three commands. The runner repo has no DuckDB build, so
set `BUILD_DIR` to a directory that holds `duckdb` and `test/unittest`. Rung 03 also needs
`LADDER_FAKE_KEY_ID` and `LADDER_FAKE_SECRET` (any values), and a build with the aws extension.

    cd examples/ladder/02_rules
    ducktest scan --env               # what each test gets, and from where
    ducktest select --explain         # what a run takes, and why
    ducktest run                      # run it

duckdb-delta and duckdb-azure do not run on this release yet. Their confs in the `Project(...)` form
exist as drafts in the maintainers' notes (design.md, Appendix B, shows delta's); the repos carry the
earlier form until they are repinned to this release.

Where a local S3 or Azure server runs, and how a run finds one, is in `README.md` ("Where services
run"). Settings of the servers themselves:

- S3, `S3ServiceDef.rustfs()`: RustFS, the one flavor built. `DUCKTEST_S3_LOCAL_*`: `PORT` (9000), `ACCESS_KEY`, `SECRET_KEY` (default
  `admin`, `password`), `REGION`, `BUCKET`, `IMAGE`. The files of an S3 storage (cleanup, a bucket's
  seed) need `rclone` on PATH. A server's buckets (`S3ServiceDef.rustfs(buckets=[bucket(...)])`) are made by
  the server's own S3 API calls.
- Azure blob, `AzureServiceDef.azurite()`: Azurite, the one flavor built. `DUCKTEST_AZ_LOCAL_*`: `PORT` (10000), `ACCOUNT`, `KEY`
  (Azurite's own `devstoreaccount1` and its well-known key when unset), `CONTAINER`, `IMAGE`.
- If Spark does not start in a container, try `JAVA_TOOL_OPTIONS=-XX:-UseContainerSupport`.

## Where to read

- `README.md`: install, and a first run in an existing repo.
- `examples/ladder/README.md`: seven small test trees. Each adds one idea: an existing repo, no
  config, rules, credentials, an HTTP service, S3, Azure blob.
- [design.md](design.md). Part 1: how to write a test. Part 2:
  how it works, for people who write resource kinds or debug the runner. Appendix B: the delta
  configuration.

## Known open

- The delta `pyproject.toml` takes the runner from a local path. On another machine, change
  `[tool.uv.sources]` before `uv sync`.
- Scan does not check plain `{NAME}` names or `ctx.client("...")` names. A typo shows only when the
  statement runs. It checks the names of declarations and `{name.attr}` (design.md, 1.9).
- One project cannot include another's conf and tests yet, nor refer to a name of another project.
- duckdb-delta and duckdb-azure still hold the earlier conf form and the runner's earlier name; they move to
  `Project(...)` and `ducktest` before they run on this release.
- After a green run, the batch directory of a `.test` batch with a skipped test stays in the run
  directory.
- Cleanup does not remove files under an s3 storage that is not on the local S3 server.
- After a killed run, cleanup does not sweep a storage or service that takes inputs.
- A `.test` batch under the own temp root of a matrix value is not under the run token, so cleanup cannot
  find it.
- A remote storage has no local twin (`LOCAL_TEMP_DIR`, `LOCAL_DATA_DIR`).
- Cleanup knows a `STORAGE.at(...)` place from the conf files, and from a body's `@test(...)` only in
  the process that scanned the body: `ducktest cleanup` of a killed run misses a place only a body names.
- The scan does not check that the storage `temp=` binds has a credential that can write to it.
- duckdb-azure's `.test` files write under `az://{AZ_TEMP_DIR}`; with the new conf they write under
  `{TEMP_DIR}` (`temp=WRITES.at("ducktest")`), and the files change with it.
- The ladder has no two-engine rung and no example of `setup` or `teardown`.
- Rung 05's golden is the BLOCKED case. A live S3 run is checked by hand, not by the self-tests.

- Two credentials of one type in one test are a scan error: not supported yet.
- Engines no longer take `AWS_*` from the shell's env. A repo whose tests relied on personal keys
  declares `S3CredentialDef.from_aws_env(...)`, and lists other names it needs (`AWS_CA_BUNDLE`) in
  `Project(inherit_env=...)`. The same holds for `AZURE_*`, `IDENTITY_*`, `MSI_*` and `IMDS_*`: declare
  `AzureCredentialDef.from_azure_env(...)`; keep `AZURE_CONFIG_DIR` (az cli login) in `inherit_env`.

## Not in this release, on purpose

- Tables per test (`TableDef`) and catalog services (Unity Catalog, Databricks).
- The `1password:` credential source. It is accepted in a declaration and refused when used.
- An HTTP proxy kind for httpfs. Credential types other than S3 and Azure (GCS, R2). Spark with an
  Azure credential. Azure DFS (`abfss://`) on a local server: Azurite has none.
- MinIO, and any choice of S3 server. RustFS is the only one; `DUCKTEST_S3_LOCAL_IMAGE` changes
  the image.
- An HTTP(S) file server and a certificate authority for the tests: they come as service kinds
  when a repo needs them.
