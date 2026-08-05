# Changelog

## dev/v0.4 - 2026-08-04

Changes since `dev/v0.3`.

### Breaking changes

- Suite matrix cells now use a required, non-empty `id`. Replace the former `backend` key.
- Removed the unused `late_fetch` credential option. Collect-first provisioning resolves the final
  pytest selection, including `-k`, and fetches required credentials before test execution.
- A suite-level `provisioner` now requires a suite `path`. It is registered for that path; provisioners
  registered for a more specific path still take precedence.
- `--temp-dir-base` has been replaced by `--temp-dir-root`. Ducktest now uses the unittest interface from
  DuckDB PR [#24355](https://github.com/duckdb/duckdb/pull/24355), including `--local-temp-dir-root`,
  `--local-data-dir`, and `--env-passthrough`.
- A custom `DUCKTEST_MINIO_IMAGE` must contain the `mc` client. MinIO administration and presigned URL
  generation run `mc` inside the service container.
- Adopted service (`to_env`) and credential (`adopt="env"`) values are no longer merged into the real
  `os.environ`. They resolve on demand, scoped to the invoking item's own suite — consume them via
  SQLLogic substitution, `run_paired`'s `env=`, or a suite-declared fixture, not bare `os.environ`.

### Added

- `env_ref(name)` late-binds a suite matrix property to the assembled invocation environment. Missing
  variables and reference cycles fail with the matrix cell and property in the error.
- Suite matrix properties can set `test_config`, `init_sql`, `temp_dir_root`, `local_temp_dir_root`,
  `data_dir`, and `local_data_dir`. Other properties become invocation environment variables.
- `run_paired(init_sql=...)` lets a paired driver supply its own preamble SQL, composed after suite
  and matrix-cell init SQL.
- `--slow` collects `.test_slow` and `.test_coverage` files. These tests receive the `slow` marker and
  run as one SQLLogic body per subprocess.
- Shared CA, HTTP/HTTPS server, and mitmproxy services. Mitmproxy profiles support `latency`, `status`,
  `fail_nth`, and credential-validating `require_auth` behavior, delivered via httpfs's
  `extra_http_headers` setting (composed with `map_concat`, not replaced).
- Configurable MinIO services, virtual-host domains, `mc` administration helpers, and presigned URLs.
- `ducktest.workspace.scratch()` provides a stable `.ducktest` directory for driver-managed service
  state.
- `ducktest build-images` builds driver-owned resource images under the references used at runtime.
- `tls` and `mitm` optional dependency groups.

### Changed

- Bare SQLLogic tests and `run_paired` now apply suite matrix configuration in the same way, including
  test configs, init SQL, data roots, temporary roots, and environment properties.
- Temporary roots are isolated by session and batch. Remote roots can have matching local counterparts;
  data roots remain stable across invocations.
- SQLLogic substitution receives only driver-owned environment bindings: service values, adopted
  credentials, matrix properties, and values passed to `run_paired(env=...)`. Other process environment
  variables remain available through SQLLogic `require-env` and `test-env`.
- `run_paired` now provisions its item's suite services itself, so a swallowed collect-first failure
  surfaces loud instead of a paired driver silently running without the service's env.
- `run_paired`'s temp-dir root now folds in the pytest node name, not just the body name and matrix
  cell. Two test functions in one driver calling `run_paired` against the same body no longer collide
  on the same `--temp-dir-root`.
- Batch affinity now considers full suite membership, not just `auto_init_sql`. Two suites sharing a
  matrix cell but declaring different resources no longer share a subprocess invocation.
- Suite provisioners are selected by path, allowing a shared provisioner with narrower overrides.
- Build-kind resource images are built once for multi-platform publication and mirrored during release
  finalization.

### DuckDB compatibility

Ducktest requires a `unittest` binary with the interface from DuckDB PR #24355. Existing SQLLogic tests
that retain `require-env` for a driver-passed variable also require
[DuckDB PR #24459](https://github.com/duckdb/duckdb/pull/24459); without it, the runner rejects the duplicate
registration.
