# ducktest on duckdb-httpfs

`ducktest_conf.py` runs the `.test` files of
[duckdb-httpfs](https://github.com/duckdb/duckdb-httpfs) with ducktest. The test files do not
change. The file goes to `test/ducktest_conf.py` in an httpfs checkout.

## Try it here

This directory is also a small project of its own: `ducktest_conf.py` sits where httpfs's `test/`
would be, with three short tests in `sql/`, one for each kind of file the conf handles:

| File | Needs | Gets it from |
| --- | --- | --- |
| `sql/client_setting.test` | nothing; runs once per `httpfs_client` | the matrix |
| `sql/s3_read_presigned.test` | `require-env S3_TEST_SERVER_AVAILABLE`; reads a seeded file | the S3 rule: keys, address, bucket |
| `sql/http_server_roundtrip.test` | `require-env PYTHON_HTTP_SERVER_URL` | the HTTP-server rule: the service |

```sh
cd examples/httpfs
BUILD_DIR=<duckdb v2.0 build with httpfs> ducktest run --root .
```

With the S3 server up, all 6 cells pass (3 files x 2 clients; 2026-10-05). Without it, the S3
file's two cells are BLOCKED, each saying which server it waited for, and the other four pass. The
HTTP server needs nothing: the run starts it.

## What it shows

- **A service with buckets.** `s3_local(buckets=[...])` is a local S3 server (RustFS) with the
  three buckets of httpfs CI. The run makes the buckets, seeds `test-bucket` from `test/s3_seed/`,
  and removes them at the end.
- **A credential, given by the file's own `require-env` line.**
  `match("**", requires_env="S3_TEST_SERVER_AVAILABLE", credential=S3_KEYS)` gives the S3 keys only
  to the files that ask for them. The keys reach the engine as `AWS_*` env. Without an S3 server,
  only these files are BLOCKED; the other files run.
- **A matrix.** The parameter `httpfs_client` in the project's `matrix=` runs each test once with curl and once with
  httplib, as two of the CI configs in `test/configs/` do. Ids end in `[httpfs_client=curl]` or
  `[httpfs_client=httplib]`; `--select httpfs_client=curl` takes one.
- **A service bound by `service=`.** A small HTTP server that the run starts in its own process,
  the same on a dev box and in CI. `{pyhttp.url}` becomes its address and `{pyhttp.dir}` the
  directory it serves, where a test writes the files it then reads over HTTP. `mutex=` stops the two
  clients from writing the same file at the same time.
- **Values, settings, process env.** `values=` fills the `{NAME}`s and `require-env` lines,
  `settings=` gives `SET` statements, `process_env=` gives the variables httpfs reads with `getenv`.

## How to run it

1. A DuckDB v2.0 (main) build with httpfs. Set `BUILD_DIR` to its `build/debug` or `build/release`.
2. In the httpfs checkout: `ducktest init`, then `uv sync`. Until the runner branch is pushed,
   point `pyproject.toml` at a local runner checkout (`path = "...", editable = true`).
3. Copy `ducktest_conf.py` and `s3_seed/` to `test/`, and add the three generated files that
   `s3_seed/README.md` lists.
4. For the S3 files: an S3 server at `DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT` (keys
   `admin`/`password`), or docker, and `rclone` on PATH.
5. `ducktest run --suite default`. `ducktest scan --env <file>` shows what a test gets.

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
- CI's `skip_tests` for httplib are not mapped here (a `suites=` entry with `skip` could do it).
- A seed copies files, not symlinks: put copies or hard links in `test/s3_seed/`.
