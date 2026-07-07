# duckdb-pytest-driver

A generic **pytest front-end over the duckdb `unittest` (Catch2) binary**. It collects
`.test` (SQLLogic) files and runs them through the binary, adds Python `initialize`/`finalize`
drivers, declarative `@requires` provisioning, managed temp dirs, and batching/parallelism. It
also aims to replace the `THIS_THING_IS_PRESENT` env-var sprawl and ad-hoc credential plumbing
with Python-side, declarative resolution (env / file / secret tools) — while keeping
`.test`/`.sql` the central artifact.

- **Distribution:** `duckdb-pytest-driver` — **import package:** `duckdb_pytest_driver`
  (a short `driver` compat alias also re-exports the public API).
- **Auto-registered** pytest plugin via a `pytest11` entry point: no `pytest_plugins` line,
  no `sys.path` hacks, no symlinks.

The full design, model, resource semantics, and roadmap live in **[`docs/`](docs/)**:
`docs/PRD.md` (why this exists), `docs/FIXTURES.md` (SQL-defined table fixtures, with
DuckDB as the middleman), `docs/EXTRACT_DRIVER_PLAN.md` (this extraction),
`docs/NOTES.md`, `docs/PLAN.md`, `docs/DISPOSITIONS.md`. A runnable pure-DuckDB fixture
demo lives in [`examples/pure-duckdb/`](examples/pure-duckdb/).

## Model

- A **test** is the set of same-stem **members** under `test/**` (e.g. `foo.test` + `foo.py`).
- A member plays role(s): a **body** (`.test` — `.sql` planned; or a `.py` carrying its own
  assertions) and/or a **driver** (`.py` — Python `initialize`/`finalize` around the body).
  Roles aren't file types: a `.py` can be both.
- A **driverless** body runs directly through the binary; a body with a same-stem `.py`
  **driver** runs only via that driver (the standalone body is suppressed).

## Install

A pytest plugin must live in the **same environment as pytest** (it's imported by the pytest
process). Pick one:

```bash
# editable / dev
uv pip install -e /path/to/duckdb-pytest-driver          # add [xdist] for -n parallelism
uv pip install -e '/path/to/duckdb-pytest-driver[xdist]'

# ephemeral, no venv mutation
uv run --with duckdb-pytest-driver pytest ...
```

## Quickstart

In a **built** duckdb (or extension) checkout:

```bash
cd <duckdb-checkout>
duck-test configure .                    # once: writes pytest.ini (settings a plugin can't inject)
pytest                                   # auto-detects rootdir + test/; finds
                                         # build/<variant>/test/unittest; collects & runs .test
pytest --build relassert test/sql/...    # options unchanged
```

Auto-detection: `working_dir` defaults to pytest's **rootdir**; the test tree to
`<rootdir>/test`. Both are overridable via ini (`duckdb_working_dir`, `duckdb_test_root`) or
CLI (`--duckdb-working-dir`, `--duckdb-test-root`).

**Test tools are a precondition.** Both the `unittest` binary and the `duckdb` CLI come
from **one build** — `build/<KIND>/{test/unittest, duckdb}`, selected by `--build <KIND>`
(or `$BUILD_DIR`); each is individually overridable with `--unittest-bin` / `--duckdb-bin`.
The resolvers are `find_binary(config, working_dir)` and `find_duckdb(config, working_dir)`;
a backend that instantiates fixtures uses the latter (see `docs/FIXTURES.md`).

## Writing tests

**Plain SQL (no Python).** Drop a `.test` under `test/sql/<area>/`; it runs through the binary
automatically:

    # test/sql/demo/answer.test
    # name: test/sql/demo/answer.test
    # group: [demo]

    query I
    SELECT 42;
    ----
    42

Run it: `pytest test/sql/demo/answer.test` (or a subtree: `pytest test/sql/demo/`).

**Add Python setup/teardown (a driver).** Put a same-stem `.py` next to the body; it becomes
the test and drives the body via `run_paired`. The standalone `.test` is then suppressed:

    # test/sql/demo/answer.py
    import pytest
    from duckdb_pytest_driver import run_paired

    @pytest.fixture
    def staged(tmp_path):
        yield tmp_path            # initialize before / finalize after the yield

    def test_answer(request, staged):
        run_paired(request)       # drives answer.test through the binary

**Declare external resources (`@requires`).** Declare the need; the backend provisioner
satisfies it and the body sees provisioned values via `${...}`. Skips cleanly when the
creds/backend are absent:

    from duckdb_pytest_driver import run_paired, requires

    @requires(source="sales", access="ro")
    def test_reads_sales(request, resources):
        run_paired(request, env=resources.env)

Provisioners + helpers live per-extension (e.g. `test/py/<repo>/`); a backend registers its
provisioner from a small conftest via `register_provisioner`. The resource model is in
`docs/NOTES.md`; the `@requires`/matrix roadmap in `docs/PLAN.md`.

**Interactive (`--repl`).** `pytest --repl <test>` provisions the single selected test's
`@requires` resources and drops into a shell attached to them (repl-kind = body-kind: a SQL
body → duckdb CLI; a pure-`.py` body → python shell, planned). Named `--repl` (not `--cli`) to
dodge pytest's `--log-cli-*` and stay body-agnostic.

## Consumer config (`duck-test configure`)

A plugin **cannot** inject `addopts` / `testpaths` / `--import-mode` / `python_files`. Run
**`duck-test configure`** once at the repo root; it writes the base `pytest.ini` (no-op if
already identical, stops with a diff if it exists and differs — never clobbers a hand-edited
copy). After that, bare `pytest` works with no other setup:

```bash
duck-test configure .        # writes ./pytest.ini
pytest                       # just works
```

The file it writes:

```ini
[pytest]
addopts = -n auto --dist=loadgroup --import-mode=importlib
testpaths = test
python_files =
```

- `--import-mode=importlib` is required so same-stem sibling drivers
  (`table-cmt/read.py` vs `table-plain/read.py`) don't collide.
- `python_files =` disables pytest's native `test_*.py` pickup — many duckdb repos ship
  non-pytest scripts named `test_*.py` that `sys.exit` at import, which crashes collection.
- **No conftest is needed for the base `.test` lane.** Only backends that provision external
  resources add a small conftest calling `register_provisioner(config, MyProvisioner())`
  (extension-specific; unchanged protocol).

## Public API

`from duckdb_pytest_driver import` (or `from driver import`): `SqlLogicFile`,
`register_options`, `find_binary`, `find_duckdb`, `has_driver`, `is_driver`, `run_paired`,
`requires`, `Requirement`, `collect_requirements`, `register_provisioner`, `get_provisioner`,
`Fixture`, `register_instantiator`, `get_instantiator`, `step`.

## CLI: `duck-test`

`duck-test configure` (above) writes the base config a plugin can't inject, so bare `pytest`
works afterward — **installing the tool + one `configure` is enough.** A future `duck-test run`
passthrough (wrapping pytest with those flags on the command line, nothing written to disk) is
the zero-file-touch variant; not built yet. **North star:** installing the test tool is enough
and it just works.

## How we got here

Background, kept for context — not needed to use the tool:

- **Why this shape.** `.test` files run SQL well but offer little setup/fixtures/matrix, so
  extensions (Delta, Iceberg, Azure) bolted external setup and non-SQL checks onto ad-hoc scripts.
  Alternatives weighed — more bash glue, an external orchestrator (Dagger/Go/Rust), extending the
  C++ `unittest` binary — lost to a thin **pytest front-end over the existing binary**, keeping
  `.test`/`.sql` central and the binary the source of truth. Full rationale: **[`docs/PRD.md`](docs/PRD.md)**.
- **`pytest-cpp` was evaluated and not used** — the driver invokes the binary and parses its
  output itself, rather than binding duckdb's (dated) Catch fork via pytest-cpp.
- The framework began **in-tree** (`test/py/driver/`, consumed by symlink) and was extracted to
  this standalone installable package; it stays import-agnostic so it can also be vendored back
  into duckdb core. See **[`docs/EXTRACT_DRIVER_PLAN.md`](docs/EXTRACT_DRIVER_PLAN.md)**.
