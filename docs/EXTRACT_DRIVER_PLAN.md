# Plan: extract the pytest driver into `duckdb-pytest-driver` (pip/uv-installable)

Design plan for lifting the driver framework out of the in-tree `test/py/driver/` and into a
standalone, installable package usable from any duckdb checkout. Written to be reversible — the
same package can still be vendored back into duckdb core (see "Dual-mode").

## Where we are today (baseline)

- **SoT:** `~/src/d/pytest/d/test/py/driver/` — 7 modules, ~1388 LoC, **pure Python**.
  `plugin.py` (704), `sqllogic.py` (325), `mnemonic.py` (116), `requires.py` (104), `provision.py`
  (78), `steps.py` (33), `__init__.py` (21).
- **Third-party deps:** `pytest` + `pytest-xdist` only. Everything else is stdlib. (`demo_gen`,
  seen in `test/py/demo/`, is demo-only via `scripts/data_generator` on sys.path — NOT a driver dep.)
- **Distribution today = symlinks.** A consumer (e.g. `uc/local/image-tests`) symlinks:
  `test/py/driver → SoT/test/py/driver`, `conftest.py → SoT/conftest.py`, `pytest.ini → SoT/pytest.ini`.
- **Registration:** `pytest_plugins = ["driver.plugin"]` in the root conftest, which also does
  `sys.path` inserts (`test/py`, `scripts`, …), owns `pytest_collect_file`, sets
  `config.sqllogic_working_dir`, and `collect_ignore = ["duckdb","build"]`.
- **Coupling to a duckdb checkout:** the *repo root* (`working_dir`) → `build/<variant>/test/unittest`
  and `test/**/*.test`. The driver invokes the Catch2 `unittest` binary as a subprocess and parses its
  output (incl. the `--emit-test-events` JSON stream). The `.test` files and the binary live in duckdb.
- **Public API** (`driver/__init__.py`, already clean): `SqlLogicFile`, `register_options`,
  `find_binary`, `has_driver`, `is_driver`, `run_paired`, `requires`, `Requirement`,
  `collect_requirements`, `register_provisioner`, `get_provisioner`, `step`.
- **Provisioner protocol** (`provision.py`) is already **extension-agnostic** and registered from a
  conftest — a perfect seam for the packaged model.

## Target model

- Standalone git repo → installable dist **`duckdb-pytest-driver`**, import pkg
  **`duckdb_pytest_driver`**.
- **Auto-registered** pytest plugin via a `pytest11` entry point — no `pytest_plugins` line, no
  `sys.path` hacks, no symlinks.
- **Zero-to-minimal consumer conftest:** the base `.test` (sqllogic) lane works with just `pytest` in a
  built checkout. Only backends needing provisioning (UC docker, …) add a ~10-line conftest.
- **Dual-mode:** keep the code import-name-agnostic so it can also be vendored into duckdb core
  (a thin `duckdb/test/py/` shim importing the same modules) — de-risks "switch back to embedding".

## Repo structure (standard src-layout, typical for pip distr)

```
duckdb-pytest-driver/
  pyproject.toml            # hatchling backend; pytest11 entry point; deps=[pytest]; extra=[xdist]
  README.md
  LICENSE
  src/duckdb_pytest_driver/
    __init__.py             # same public API re-exports
    plugin.py
    sqllogic.py
    provision.py
    requires.py
    steps.py
    mnemonic.py
  tests/                    # the DRIVER's own tests (plugin behavior vs a *stub* unittest binary)
  docs/                     # optional home for NOTES.md / DISPOSITIONS.md if they move here
```

`pyproject.toml` essentials:
```toml
[project]
name = "duckdb-pytest-driver"
requires-python = ">=3.8"
dependencies = ["pytest>=7"]
[project.optional-dependencies]
xdist = ["pytest-xdist"]
[project.entry-points.pytest11]     # <-- auto-registration; replaces pytest_plugins
duckdb_driver = "duckdb_pytest_driver.plugin"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```
Import rename `driver` → `duckdb_pytest_driver`: internal imports are already relative (`from .x`), so
only the `__init__` docstring and any absolute `from driver import …` in consumer conftests/tests change.

## Install & use in an arbitrary duckdb checkout

A pytest plugin must live in the **same environment as pytest** (it's imported by the pytest process —
not a standalone CLI, so plain `uvx`/tool-install won't wire it in). Options:
- **Editable/dev:** `uv pip install -e /path/to/duckdb-pytest-driver` into the project venv.
- **Pinned:** add to the project's dev deps, or `uv pip install duckdb-pytest-driver` (PyPI / `git+https`).
- **Ephemeral, no venv mutation:** `uv run --with duckdb-pytest-driver pytest …`.

Then, in a built checkout:
```
cd <duckdb-checkout>
pytest                                   # plugin auto-registers; auto-detects rootdir + test/;
                                         # finds build/<variant>/test/unittest; collects & runs .test
pytest --build relassert test/sql/...    # existing options unchanged
```
- **Auto-detection:** `working_dir` defaults to pytest's rootdir; test root to `<rootdir>/test`; both
  overridable via ini (`duckdb_working_dir`, `duckdb_test_root`) or CLI. `--build`/`$BUILD_DIR`/
  `--unittest-binary` resolution is unchanged.
- **Provisioning backends:** consumer conftest calls `register_provisioner(config, MyProvisioner())`
  (extension-specific, optional).

## Blast radius — how widespread are the changes? (SMALL & localized)

Driver code is **barely touched**. The work is scaffolding + relocating conftest responsibilities.

1. **Packaging scaffold** (new): `pyproject.toml`, `src/` move, package rename, `pytest11` entry point.
2. **Fold the root-conftest logic into `plugin.py`** so the base lane needs no consumer conftest:
   - `pytest_collect_file` → plugin hook (gated on test_root + `.test`/driver-`.py`).
   - `sqllogic_working_dir` → computed from rootdir/ini (keep the attr name for compatibility).
   - `collect_ignore` → a plugin can't set that var; use the `pytest_ignore_collect` hook instead
     (ignore `duckdb/`, `build/`).
   - `register_options` → already a plugin `pytest_addoption`; drop the consumer pass-through.
   - `sys.path` inserts → GONE (installed package).
3. **Ini defaults currently in the symlinked `pytest.ini`** (`addopts = -n auto --dist=loadgroup
   --import-mode=importlib`, `testpaths = test`, `python_files =` to disable native `.py` collection):
   a plugin **can't** inject `addopts`/`testpaths`. Two choices — (a) ship a documented ~5-line
   consumer `pytest.ini` stub, or (b) have the plugin own more of collection (disable native `.py`
   pickup via hooks; enforce loadgroup batching in the collector) to shrink the stub toward zero.
   Recommend starting with (a); tighten later. **This stub is the main residual consumer-side config.**
4. **Decouple demo/`demo_gen`** — demo-only; leave out of the package (ship as an example under
   `tests/` if useful).
5. **Consumers:** delete the 3 symlinks; add the package to dev deps; optional minimal conftest
   (provisioner) + optional ini stub.

**Does NOT change / move:** the `.test` files and the `unittest` binary stay in duckdb; the public API
surface is identical (provisioners/drivers keep working); the provisioner protocol already fits.

## Risks / watch-items

- **Env coupling:** plugin must be in the pytest venv (the `uvx` caveat) — document `uv run --with`.
- **`addopts`/`testpaths` can't come from a plugin** → residual consumer ini (small, documented).
- **`--import-mode=importlib` is required** for same-stem sibling drivers (`table-cmt/read.py` vs
  `table-plain/read.py`); that's a consumer ini choice — document it.
- **rootdir ≠ repo root** in some invocations → always provide an explicit override.
- **Binary contract coupling:** the driver assumes unittest flags (`--emit-test-events`, `--temp-dir-*`,
  `--select-tag`, …). A standalone release must pin/document a **min duckdb (unittest) version**.
- **Keep import-agnostic** so a future vendor-into-core drop-in still works.

## Migration steps (empty repo → working package)

1. `git init`; add `pyproject.toml` (hatchling + `pytest11`), `src/duckdb_pytest_driver/`, README, LICENSE.
2. Copy the 7 modules; rename package; fix any absolute `from driver import`.
3. Move `pytest_collect_file` + working_dir/test_root + ignore logic into `plugin.py`, ini/CLI-driven
   with auto defaults.
4. Add self-tests under `tests/` using a **stub** unittest binary (a shell script emitting canned
   Catch2 / `[TEST_EVENT]` JSON), so the package's CI needs no real duckdb build.
5. Publish (PyPI or git tag). In consumers: `uv pip install …`, delete symlinks, add optional
   conftest/ini.
6. (Optional) keep a thin vendored shim (`duckdb/test/py/` importing the same modules) for the
   embed-in-core path.

## Decisions (resolved)

- **Publish target:** start **local/private** (editable install / `git+https`). Defer PyPI.
- **Names:** distribution `duckdb-pytest-driver`, import `duckdb_pytest_driver` (optional short `driver`
  alias). Rationale: the *distribution* name is separate from the *import* name and appears only in the
  install command / `pyproject.toml` / entry point / PyPI URL — not in test code. It's free to rename
  until first publish and ~permanent after (PyPI immutability), so lock the full name in now. Keep the
  *import* name short & stable since that's what code references.
- **Distribution model = dual-mode.** One import-agnostic codebase that works both pip-installed AND
  vendored into duckdb core (`duckdb/test/py/`). The standalone repo is the current direction; the
  embed-in-core option stays cheap to reach. (Supersedes nothing; preserves the "switch back" path.)
- **Collection / ini — the tool owns config.** A plugin *can't* inject `addopts`, `testpaths`,
  `--import-mode=importlib`, or clear `python_files` (import-mode + python_files are consumed too early;
  the others are awkward). So the tool provides them via a **console-script CLI, `duck-test`**
  (`duck-test = duckdb_pytest_driver.cli:main`). **BUILT:** `duck-test configure [dir]` writes the base
  `pytest.ini` (no-op if identical; stops with a diff if it exists and differs — never clobbers a
  hand-edited copy). After one `configure`, **bare `pytest` just works**. A future `duck-test run`
  passthrough (flags on the command line, nothing written) is the zero-file-touch variant — not built.
  **North star = "installing the tool + one `configure` is enough."** Verified end-to-end against a
  merged `~/src/d/d` checkout: raw bare `pytest` fails (native `test_*.py` collection imports a
  `sys.exit`-ing script → INTERNALERROR); after `duck-test configure .`, collection is clean (4382
  scoped to `test/`) and a `test/sql/catalog/function` slice runs green (32 passed) via plain `pytest`.
- **Docs:** move the framework docs (`NOTES.md`, `DISPOSITIONS.md`, `PLAN.md`, this plan) INTO the new
  repo under `docs/`. The new repo's `README.md` is a skeletal quickstart that points at them. The
  duckdb-side `test/README.md` env-var *contract* stays in duckdb (it documents the unittest binary's
  guarantees, not the driver) but gains a pointer to the driver repo.

## Still open / TBD

- **Version contract:** how to pin the min duckdb (unittest) version the driver targets — real issue,
  mechanism TBD (e.g. a probe of `unittest --version`/feature flags, or a documented floor).
