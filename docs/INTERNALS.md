# Internals (for driver devs/extenders)

How `duckdb-pytest-driver` is built and where to extend it. Assumes you've read
**[ARCHITECTURE.md](ARCHITECTURE.md)** (the integrator model). This doc is the map under the hood.

## Package + install

- **Import name `ducktest`** (single name — the old `duckdb_pytest_driver` + `driver` shim are gone).
  Distribution is still `duckdb-pytest-driver` (dist name ≠ import name). Public API is re-exported from
  `__init__.py`; keep `__all__` current so `from ducktest import *` and the docs stay accurate.
- **Auto-registration** via the `pytest11` entry point `ducktest = ducktest.plugin`.
  No `pytest_plugins`, no `sys.path` hacks.
- **`ducktest configure`** (`cli.py`) writes the base `pytest.ini` a plugin can't inject
  (`addopts`, `testpaths`, `--import-mode`, `python_files`). Config the tool owns lives there — don't
  try to smuggle it into the plugin.
- Must work both pip-installed **and** vendored into duckdb core (`duckdb/test/py/`): keep imports
  relative, keep the public API in `__init__.py` stable.

## Module map

```
plugin.py     the pytest hooks + the harness (run_paired, find_binary); suites/store wiring
suites.py      register_suite / credential / service / use_service + the frozen descriptors (declaration only)
store.py      the process-shared store: SyncManager server + per-key state machine
provision.py  path-scoped provisioner registry (populated by register_suite or an explicit nested override) + base Provisioner/Bindings
requires.py   @requires / @requires_matrix + Requirement (per-test table needs)
fixtures.py   TableSpec SQL-definition model + instantiate via the duckdb CLI (the `resources` path)
sqllogic.py   SqlLogicFile — collect a .test + run it through the unittest binary
sqldef.py     generic multi-statement SQL helpers   steps.py  step() narration   mnemonic.py  tokens
```

## Hook map + ordering

| hook | where it runs | what it does | ordering |
|---|---|---|---|
| `pytest_load_initial_conftests` | controller | registers the collect-first `Controller` | before `pytest_configure` |
| `pytest_configure` (module) | controller + workers | working dir, markers, forces `-n0` for `--repl`/`--steps` | **`tryfirst`** — must set `numprocesses` before xdist reads it |
| `Controller.pytest_configure` | controller | starts the store after consumer suite registration | **`trylast`** |
| `Controller.pytest_collection` | controller | performs collect-first, builds/emits the plan, reconciles, provisions reachable resources | **`tryfirst`** |
| `pytest_collection_modifyitems` | controller + workers | auto-marker, default deselection, matrix expansion, dedup, batching | **`hookwrapper`** — pre-yield state must exist before pytest/xdist selection hooks |
| `pytest_collection_modifyitems` | workers | provisions resources for the worker's selected suites and adopts their environment | post-yield, after final selection |
| `pytest_report_header` | controller | the **suite banner** (always) + `-v` tool trace | banner ungated |
| `pytest_configure_node` | controller | broadcast the **run-id** to each worker via `workerinput` | at worker spawn |
| `pytest_sessionfinish` | controller | **stop services** then shut the store down; drop the run temp dir | — |

`Controller` is a separate plugin object because its `pytest_configure` must run `trylast`, after the
consumer conftest registers suites, while module-level `pytest_configure` must run `tryfirst` to adjust
xdist options. Registering it from `pytest_load_initial_conftests` places both hooks in normal ordering.

**Why the collection auto-marker is a hookwrapper:** pytest's builtin `-m`/`-k` deselection is itself a
plain `pytest_collection_modifyitems`. A hookwrapper's pre-yield runs before every plain impl, so the
suite markers exist *before* `-m <suite>` filters — that's what makes `-m rest` select markerless `.test`
bodies.

## The store (`store.py`)

- **Server:** a `SyncManager` subclass registering one `_Store` singleton. Started with `address=None`
  → the platform-native family (AF_UNIX / AF_PIPE, no TCP port); default start context (spawn on
  macOS/Windows, fork on Linux) — so the module must stay **import-clean** (the spawned server
  re-imports it). Security is a non-goal (local socket; authkey only because multiprocessing requires
  one).
- **Delivery:** `start_server()` → `(mgr, address, authkey)`; the controller `to_env(...)`s those into
  `os.environ` pre-fork; workers `from_env()` + `connect()`. `get_store(config)` returns the
  controller's proxy or lazily connects on a worker.
- **State machine (`_Store`):** `_values` (SET blocks) + `_errors` (FAILED msgs) + `_state`
  (`pending|set|failed`), guarded by one lock. `begin(key)` atomically claims PENDING and returns a
  role (`owner|set|failed|wait`). `copy_or_provision` polls `begin` to a terminal state:
  owner runs the factory then `set`/`fail`; a `wait` polls (2 min default) → `ProvisionTimeout`; a
  `failed` → `ProvisionFailed` (poison). `put`/`copy` are the direct eager-write / read.
- Values are stored **JSON-serialized**, so the shared value is an un-aliasable str and every `copy`
  is a fresh private dict.

## Resolution flows, in code

- **Plan** — `Controller.pytest_collection` calls `session.perform_collect()`, then `_build_plan` derives
  reachable suites and resource keys from the selected items. This includes `-k`; no selection predictor
  or per-test resource path exists.
- **Credentials** — `provision_reachable` calls `_fetch_or_read_credential` once per reachable key.
  `available()` may return early; otherwise `store.copy_or_provision` runs `fetch`, then `validate` and
  `adopt="env"`. Workers repeat the pass after their final collection to adopt environment locally;
  store single-flight prevents another fetch.
- **Services** — `provision_reachable` calls `provision_service`. Managed services use
  `copy_or_provision(key, _boot_and_populate)`; existing and start-less services use `_attach_service`
  and are not stored or stopped. `depends_on` provisions dependencies first. `to_env` is adopted in each
  provisioning process and registered for SQLLogic substitution. `_stop_services` stops stored managed
  services in reverse dependency order.
- **Selection** — `_reachable_suites(config, items)` uses actual selected item membership plus default
  suites for a bare invocation. `pytest_collection_modifyitems` applies suite markers and default
  deselection before pytest's selection hooks, then workers provision from the surviving items.

## Collection pipeline

`pytest_collect_file` (`plugin.py`) collects `.test` bodies by default and adds `.test_slow` and
`.test_coverage` with `--slow`. Slow/coverage items receive the `slow` marker before pytest applies
`-m` and run in one-test batches. For a `.test`, a same-stem `.py` suppresses the body and
`run_paired` invokes it through the driver. Slow paired drivers are not supported; an adjacent `.py`
does not suppress a slow body. Batching groups adjacent same-binary/same-workdir `SqlLogicItem`s under
an `xdist_group` marker (with `--dist=loadgroup`) so a batch runs on one worker.

## Extending

- **A new resource class** — add a descriptor to `suites.py`, include it in `Plan`, and handle it from
  `provision_reachable` and teardown. Reuse `copy_or_provision` for invocation-wide single-flight state.
- **A new test lane** (`.sql`, `.py`-only) — extend `pytest_collect_file`'s gate + the `has_driver` /
  `is_driver` pairing; see the `# PLANNED` markers in `plugin.py` and PLAN.md.
- **A shared `resources` library** — ready-made `service()`/`credential()` descriptors (minio/azurite/
  docker + s3/1Password) live in `ducktest.resources` (azurite shipped) for any backend to import; object-store seed/clean via `ducktest.tools.rclone`.

## Testing the driver (offline, always)

- Self-tests run against a **stub `unittest` binary** (`tests/conftest.py`) — **no real duckdb build,
  no network, no `op`, no docker**. Prefer offline verification; never invoke wrappers that pop
  credential prompts.
- Cross-process behavior (controller/worker split, single-flight, poison, banner, deselection) is
  tested by running a tiny inner suite via **`pytester` subprocess under `-n 2`** (see
  `tests/test_store.py`, `test_credentials_store.py`, `test_services_store.py`, `test_suite_selection.py`).
  Use a fixed `-n 2` there for deterministic controller+2-workers assertions (live runs use `-n auto`).
- **Ruff** is the linter (line length 120): `ruff check .` before calling work done. Keep `store.py`
  import-clean (spawn).

## Ground rules

Commits are the human's call (stage + show a diff, don't commit). No secrets in files (use `${VAR}`).
`docs/` is canonical for design; change behavior → update the doc that owns it. The env-var *contract*
(`TEMP_DIR`, `{TEST_DIR}`, …) is owned by duckdb's `test/README.md`, not here.
