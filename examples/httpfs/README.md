# ducktest5 on duckdb-httpfs

`ducktest_conf.py` runs the `.test` files of
[duckdb-httpfs](https://github.com/duckdb/duckdb-httpfs) with ducktest5. The test files do not
change. The file goes to `test/ducktest_conf.py` in an httpfs checkout.

## What it shows

- **A service with buckets.** `s3_local(buckets=[...])` is a local S3 server (RustFS) with the
  three buckets of httpfs CI. The run makes the buckets, seeds `test-bucket` from `test/s3_seed/`,
  and removes them at the end.
- **A credential, given by the file's own `require-env` line.**
  `match("**", requires_env="S3_TEST_SERVER_AVAILABLE", credential="minio")` gives the S3 keys only
  to the files that ask for them. The keys reach the engine as `AWS_*` env. Without an S3 server,
  only these files are BLOCKED; the other files run.
- **A matrix.** The parameter `http_client` in `MATRIX` runs each test once with curl and once with
  httplib, as two of the CI configs in `test/configs/` do. Ids end in `[http_client=curl]` or
  `[http_client=httplib]`; `--select http_client=curl` takes one.
- **A service bound by `service=`.** The Python HTTP server of CI, which the run uses but does not
  start. `{pyhttp.url}` becomes its address. `mutex=` stops the two clients from writing the same
  file at the same time.
- **Values, settings, process env.** `values=` fills the `{NAME}`s and `require-env` lines,
  `settings=` gives `SET` statements, `process_env=` gives the variables httpfs reads with `getenv`.

## How to run it

1. A DuckDB v2.0 (main) build with httpfs. Set `BUILD_DIR` to its `build/debug` or `build/release`.
2. In the httpfs checkout: `ducktest5 init`, then `uv sync`. Until the runner branch is pushed,
   point `pyproject.toml` at a local runner checkout (`path = "...", editable = true`).
3. Copy `ducktest_conf.py` to `test/`. Put `attach.db` from `scripts/generate_presigned_url.sh` in
   `test/s3_seed/presigned/`.
4. For the S3 files: an S3 server at `DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT` (keys
   `admin`/`password`), or docker, and `rclone` on PATH.
5. For the 4 files with `require-env PYTHON_HTTP_SERVER_URL`: `python3 -m http.server 8008` in
   `PYTHON_HTTP_SERVER_DIR` (default `/tmp/python_test_server`).
6. `ducktest5 run --suite default`. `ducktest5 scan --env <file>` shows what a test gets.

## Measured, 2026-10-05

httpfs @ 547feba, a debug build of DuckDB fc4002c6 with httpfs, RustFS at 172.18.0.1:9999.
`--suite default`: 105 tests per client.

| client | passed | failed | skipped |
| --- | --- | --- | --- |
| curl | 82 | 18 | 5 |
| httplib | 78 | 22 | 5 |

None of the failures come from ducktest:

- 9 use bucket host names (`test-bucket.<host>`). CI adds them to `/etc/hosts`.
- 5 read public AWS buckets. There is no internet here.
- 3 need bucket versioning, and 1 needs a second S3 user. The local server has neither.
- httplib only: 2 connection-caching tests. CI skips them for httplib.
- httplib only: 2 files find what the curl run left in the bucket. CI gives each config a new
  server.

The 5 skipped tests need a proxy, presigned URLs, a local extension repository, or
`TEST_PERSISTENT_SECRETS_AVAILABLE`; this conf gives none of them.

With no S3 server (`DUCKTEST_S3_LOCAL_PORT=9998`): 72 BLOCKED. These are the 36 files with
`require-env S3_TEST_SERVER_AVAILABLE`, for each client, and no other file.

## Known gaps

- `process_env` does not fill `{s3.host_port}` as `values` do, so the conf builds the address itself.
- No bucket host names on the local server, so the conf sets `s3_url_style=path` for every test.
- No bucket versioning, second user or presigned URLs on the local server.
- CI's `skip_tests` for httplib are not mapped here (a `SUITES` entry with `skip` could do it).
- No HTTP server with a local directory: the Python HTTP server must run before the run starts.
- A seed copies files, not symlinks: put copies or hard links in `test/s3_seed/`.
