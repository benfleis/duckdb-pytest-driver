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
suites.py      register_suite / credential / service + the frozen descriptors (declaration only)
store.py      the process-shared store: SyncManager server + per-key state machine
provision.py  register_provisioner / get_provisioner (backend provisioner registry, by test path)
requires.py   @requires / @requires_matrix + Requirement (per-test table needs)
fixtures.py   Fixture SQL-definition model + instantiate via the duckdb CLI (the `resources` path)
sqllogic.py   SqlLogicFile — collect a .test + run it through the unittest binary
sqldef.py     generic multi-statement SQL helpers   steps.py  step() narration   mnemonic.py  tokens
```

## Hook map + ordering (the tricky part)

Selection, credentials, and services are spread across several hooks with **deliberate ordering**.
The ordering is the thing most likely to bite an extender:

| hook | where it runs | what it does | ordering |
|---|---|---|---|
| `pytest_load_initial_conftests` | controller | **registers `_SuiteController`** as a plugin | must register here so its `trylast` joins normal ordering |
| `pytest_configure` (module) | controller + workers | working dir, markers, forces `-n0` for `--repl`/`--steps` | **`tryfirst`** — must set `numprocesses` before xdist reads it |
| `_SuiteController.pytest_configure` | controller only | start the **store** (pre-fork) + eager-fetch credentials + eager-provision services (`_provision_eager_services`) | **`trylast`** — runs *after* the repo's `test/conftest.py` has `register_suite`'d |
| `pytest_collection_modifyitems` (in `_SuiteController`) | workers (+ `-n0` controller) | **auto-marker + default-scan deselect** | **`hookwrapper`** — pre-yield runs before pytest's builtin `-m`/`-k` deselection |
| `pytest_collection_modifyitems` (module) | workers | dedup + xdist_group **batching** | plain (yield phase) |
| `pytest_report_header` | controller | the **suite banner** (always) + `-v` tool trace | banner ungated |
| `pytest_runtest_setup` | the executing process | the credential **backstop** (store → available → late-fetch → fail) | — |
| `pytest_configure_node` | controller | broadcast the **run-id** to each worker via `workerinput` | at worker spawn |
| `pytest_sessionfinish` | controller | **stop services** then shut the store down; drop the run temp dir | — |

**Why `_SuiteController` is a separate plugin object:** the suite logic needs `trylast` (run after the
consumer conftest registers suites), but the module-level `pytest_configure` needs `tryfirst` (set
`numprocesses` first). One module can't have both, so the suite logic lives on a second plugin object
registered in `pytest_load_initial_conftests` — registering *there* (pre-configure) is what lets its
`trylast` hook join the normal ordering and fire after conftests. (Registering mid-`configure` is too
late — verified.)

**Why the collection auto-marker is a hookwrapper:** pytest's builtin `-m`/`-k` deselection is itself a
plain `pytest_collection_modifyitems`. A hookwrapper's pre-yield runs before every plain impl, so the
suite markers exist *before* `-m <suite>` filters — that's what makes `-m rest` select markerless `.test`
bodies.

**LIFO note:** the repo's `test/conftest.py` is registered *after* the entry-point plugin, and pluggy
runs same-suite hooks last-registered-first, so the consumer's `register_suite` already ran by the time
`_SuiteController` (trylast) reads suites — the `trylast` is belt-and-suspenders on top of that.

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

- **Eager creds** — `_SuiteController.pytest_configure` → `_fetch_credentials`: for each `_suite_reachable`
  suite's creds, `fetch` → `validate` (else `pytest.UsageError`, red/exit-4, pre-fork) → `store.put` +
  `os.environ.update` when `adopt=="env"`.
- **Backstop** — `pytest_runtest_setup`: for a selected item in a credentialed suite, `store.copy` →
  `available()` → `copy_or_provision(late_fetch)` → `pytest.fail(pytrace=False)`. `_late_fetch` validates
  and raises `ProvisionFailed` on bad creds (poisoning the key).
- **Services** — `provision_service` → `copy_or_provision(key, lambda: svc.start(config))`;
  `_stop_services` (sessionfinish) stops each service whose block is present in the store (presence ==
  started), before `mgr.shutdown()`.
- **Selection** — `_suite_reachable(config, suite)` is the single from-args gate (bare+default /
  path-intersect / `-m` via pytest's `Expression`); `_default_scan_deselect` and the banner reuse it, so
  the deselect decision and the eager-fetch decision agree.

## Collection pipeline

`pytest_collect_file` (`plugin.py`) → for a `.test`: if it `has_driver` (same-stem `.py`) it's
suppressed (the driver runs it); else it becomes a `SqlLogicFile` that runs it through the binary. A
`.py` driver `is_driver` (same-stem `.test`) and is collected as a normal module; `run_paired` finds
its sibling `.test` and invokes the binary. Batching groups adjacent same-binary/same-workdir
`SqlLogicItem`s under an `xdist_group` marker (with `--dist=loadgroup`) so a batch runs on one worker.

## Extending

- **A new resource class** — add a descriptor to `suites.py`, thread it through `register_suite`, and
  wire its lifecycle into `_SuiteController` / `pytest_runtest_setup` / `_stop_services`. Reuse the store
  (`copy_or_provision` for lazy/single-flight, `put`/`copy` for eager).
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
