# ducktest5

DuckDB and its extensions are tested mostly with `.test` files: SQL statements and their expected
results, in the SQLLogic format. ducktest5 runs these files through DuckDB's own `unittest` binary,
and it runs Python test bodies for what SQL alone cannot set up: a second engine, a server, a
credential. Your `.test` files stay as they are. When the tests need more, one file,
`ducktest_conf.py`, declares it; ducktest5 makes it, gives it to the tests as `{NAME}` values, and
removes it after the run. It does not use pytest.

This repo also holds the older pytest plugin `ducktest`; see
[Legacy: the pytest plugin `ducktest`](#legacy-the-pytest-plugin-ducktest).

## Install

The package is `duckdb-pytest-driver`, and it gives the command `ducktest5`.

For a repo with only `.test` files, such as duckdb core, install it as a tool:

    uv tool install "duckdb-pytest-driver @ git+https://github.com/benfleis/duckdb-pytest-driver@dev/v0.5"

For a repo with Python test bodies, install it into the repo's test environment, with the packages
the bodies import:

    uv pip install "duckdb-pytest-driver @ git+https://github.com/benfleis/duckdb-pytest-driver@dev/v0.5"

From a local checkout of this repo, editable, so that your changes here take effect at once:

    uv tool install --editable /path/to/duckdb-pytest-driver
    uv pip install -e /path/to/duckdb-pytest-driver

## A first run, in duckdb core

Build duckdb (`make debug`), then run from the top of the checkout:

    ducktest5 run test/sql/aggregate --suite default

You need no config file. ducktest5 finds the tests in `test/` and the build in `build/debug/`. For
another build, use `--build release`, or set `BUILD_DIR` to the directory that holds `duckdb` and
`test/unittest`.

- **Select by path.** A path selects every test under it, also the `.test_slow` files.
  `--suite default` leaves the slow files out, as a bare `ducktest5 run` does. Without it, the
  example above takes more than ten minutes.
- **Test ids.** A test id is relative to `test/`, for example
  `sql/aggregate/qualify/test_qualify.test::test_qualify`. A path is relative to where you are, so
  it starts with `test/`. `--id <test id>` runs one test.
- **Results.** Each test gets one line: PASSED, FAILED, ERROR, SKIPPED or BLOCKED (a service it
  needs did not start). A failure prints what is different, what was kept, and the command that
  runs it again.
- **SKIPPED** means that a `require` in the file was not met: an extension that is not built, or an
  environment variable that is not set. The whole file is SKIPPED, also when statements above the
  `require` ran. DuckDB's `test/run` counts such a file as passed, so the counts can be different.
  A Python body skips with `ctx.skip(reason)`, or `ducktest5.skip(reason)` from a helper.
- **Speed.** `--workers N` (default 4) runs N batches at the same time. A batch is one `unittest`
  process with up to 10 `.test` files. For a big run, `--level terse` prints only the summary.
- **Diagnostics.** `-v` and `-vv` print ducktest5's own diagnostics on stderr, at INFO and DEBUG.

Each run writes a run directory, `duckdb_unittest_tempdir/<run>/`, in the working directory. The
last line of the output gives its path. It holds `01-tests.yaml` (the tests and what each gets),
`02-plan.yaml` (the batches), `03-provision.yaml` (what was made), `04-events.jsonl` (what
happened), `05-report.json` and `05-report.yaml` (the results), and `ducktest.log` (diagnostics at
DEBUG). `ducktest5 report --run-dir <run dir>` prints the results again; `--junit FILE` also writes
JUnit XML for CI.

To look before you run:

    ducktest5 select test/sql/aggregate --suite default    # which tests run, and why the others do not
    ducktest5 scan --env test/sql/aggregate                # what each test gets, and from where

An extension repo works the same way: build it, then run from the top of its checkout. When its
tests need a service, a storage or a credential, write a `ducktest_conf.py`; the ladder and the
design page show how.

## The phases

`ducktest5 run` does all phases in one process:

1. **scan**: find the tests and what each needs.
2. **plan**: put the tests into batches.
3. **provision**: start the services and make the storages.
4. **exec**: run the batches.
5. **cleanup**: remove what the passed tests wrote, and stop the services.
6. **report**: print the results.

Each phase is also a command of its own (`ducktest5 scan`, `plan`, `provision`, `exec`,
`cleanup`, `report`), which reads and writes a run directory. Use them to start services in one
place and run the tests in another, or to report a run again.

## Read next

- [examples/ladder/](examples/ladder/README.md): small test trees, from an existing repo with no
  config (rung 00) to a local S3 server (rung 05). Each rung adds one idea.
- [docs/ducktest5/design.md](docs/ducktest5/design.md): how to write a test (Part 1), how it works
  (Part 2), and how to move an existing repo (1.6).
- [docs/ducktest5/status.md](docs/ducktest5/status.md): what works today, and what is open.
- `ducktest5 <command> --help`.

## Develop ducktest5

The self-tests run against fake binaries. With `DUCKDB_SHELL` set to a built `duckdb` that has
`test/unittest` beside it, the ladder runs on that build too:

    DUCKDB_SHELL=<a duckdb shell> uv run --all-extras --group dev pytest -q tests/test_ducktest5_*.py
    uvx ruff check .

---

## Legacy: the pytest plugin `ducktest`

The pytest plugin is the older runner. It stays in this repo for now; new work goes into ducktest5.

DuckDB and its extensions are mostly tested with `.test` files: plain-text scripts of SQL
statements paired with their expected results, in the SQLLogic format DuckDB borrowed from
SQLite. DuckDB's own C++ test runner (the `unittest` binary) executes them, and there are
thousands. They hold up well until a test needs something SQL alone can't set up: a running
catalog server, a table in a particular storage layout, a credential, an assertion on something
other than the query result.

ducktest runs those same `.test` files through pytest, and lets you back any of them with a Python
file when you need one. Existing `.test` files keep working as they are. What you gain is pytest's
test selection and parallel runs, plus a place to put the setup that otherwise ends up in shell
scripts and manual steps.

There are three shapes of test, and one suite can mix them:

- A `.test` on its own, run straight through the binary. No Python.
- A `.test` with a same-name `.py` beside it. The `.py` prepares what the test needs (a table, a
  service, environment variables), runs the body, and can add its own Python assertions.
- A plain pytest test in Python, with no SQL, when that reads better.

It installs as a pytest plugin, so there's no separate runner: you run `pytest`.

The plugin's design and internals are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (the model),
[docs/INTERNALS.md](docs/INTERNALS.md) (extending it), [docs/MATRIX.md](docs/MATRIX.md) (running one
`.test` body across cells — backends, configs), and [docs/PLAN.md](docs/PLAN.md) (the roadmap).

### Collecting Python-native tests

The first two shapes work with no setup. A plain pytest test (a `.py` with no `.test` beside it)
needs one config line: set `python_files = test_*.py` in your `pytest.ini` so pytest collects it.
That's off by default because duckdb core ships many non-test `test_*.py` scripts that would
otherwise get swept in; a clean extension repo just turns it on. Name these files `test_*.py` to keep
them apart from the `<stem>.py` drivers that pair with a `.test`. Once collected they behave like any
other test, so suites, credentials, and `@requires` all apply.

---

### Getting started

pytest imports your test modules, so pytest, this plugin, and everything your tests import all have
to live in one environment. For a suite with real dependencies (pyspark, a SQL connector, your own
package), install them together:

```bash
# a venv holding EVERYTHING pytest needs to import
uv venv && source .venv/bin/activate
uv pip install -e /path/to/duckdb-pytest-driver pytest pytest-xdist \
    pyspark databricks-sql-connector <your test deps>
pytest

# or let uv build the env from your test project's declared deps
uv run --group test pytest        # `test` group = pytest, pytest-xdist, the driver, pyspark, …
```

The package is published as `duckdb-pytest-driver` but imports and runs as `ducktest`, the same split
as `pillow` and `PIL`. If you have an older build installed, reinstall to pick up the `ducktest`
command.

`uv tool install pytest` gives you a dependency-free install, which is fine for a pure-`.test` suite
with nothing imported beyond the stdlib. That environment is isolated, though, so it won't see test
deps installed elsewhere. As soon as your tests import a third-party package, use a venv or project as
above.

```bash
# only for dependency-free suites (no imports beyond the stdlib):
uv tool install pytest --with pytest-xdist --with-editable /path/to/duckdb-pytest-driver
```

Then, in a **built** duckdb (or extension) checkout:

```bash
ducktest configure           # once: writes pytest.ini (testpaths, -n auto, importlib — a plugin can't
                             # inject these) AND a starter pyproject.toml (the test venv, below). Re-run
                             # to refresh; it won't clobber a hand-edited pytest.ini, and never rewrites
                             # pyproject.toml once it exists — that one is yours.
pytest                       # auto-detects rootdir + test/, resolves build/<variant>/test/unittest,
                             # collects & runs every .test
```

Tools come from one build (`build/<KIND>/{test/unittest, duckdb}`), selected with `--build <KIND>` (or
`$BUILD_DIR`); override them individually with `--unittest-bin` / `--duckdb-bin`.

#### Preset credentials in the environment

For a suite that needs credentials, ducktest fetches them once, up front, and prompts your secret tool
(say `op`) only for what's actually missing. To skip the prompt entirely, which you want under
`-n auto` and for benchmarks where a pause mid-run hangs the whole thing, put the credentials in the
environment before you start:

```bash
source your-databricks-env.sh && pytest -m databricks     # env pre-populated → no prompt
op run --env-file=creds.env -- pytest -m databricks       # or let your secret tool populate it
```

The `available()` check finds them and skips the fetch. When a run does have to prompt, it happens at
the start rather than partway through, and a credential that's genuinely missing stops the run with a
clear message instead of quietly skipping the test.

---

### The Python layout that makes this work

Everything pytest imports — the driver, your `.py` drivers, backend helper packages, generators, and
their third-party deps — has to resolve from **one** environment and import the **same way regardless of
how pytest is launched**. Two anchors get you there, both at the repo root.

**One dependency set, at the repo root.** pytest, `pytest-xdist`, the driver, and every test dep go in a
single manifest at the top of the repo. `ducktest configure` scaffolds it for you — a starter
`pyproject.toml` with a `[dependency-groups] dev`, `[tool.uv] package = false` (an extension isn't an
installable package, but you still get `uv run pytest`), and a `[tool.uv.sources]` pointing at your local
driver checkout until it's published. Fill in your deps and that's the venv. Put deps anywhere else — a
`scripts/requirements.txt`, a per-suite file — and "which venv, installed from where" becomes a guessing
game. It's the repo's test venv; it belongs where you `cd` to run it, next to `pytest.ini`. Then, from the
repo root, `uv run pytest` builds the venv and runs — the same command in the driver and every extension.

**Import roots are declared, not inferred from your shell.** `from mypkg.helpers import …` resolves only
if `mypkg`'s parent is on `sys.path`, and by default that often happens *only* because you ran
`python -m pytest` from the repo root, which injects the cwd. Run bare `pytest`, or from another
directory, and the import breaks — a fragility that hides until someone launches it differently. ducktest
removes the guesswork: the **`duckdb_pythonpath`** ini option lists repo-relative dirs it prepends to
`sys.path` at startup, the same way every launch and every cwd. Its default already covers the usual
homes — `test/py scripts scripts/data_generator`, each added only if it exists — so you set it *only* to
change it, never to make the common case work.

**Where test-support Python lives is then a genuine choice, and both are fine:**

- `test/py/<pkg>/` — the default home for driver-side helpers (provisioners, service/credential code).
  Imports as `<pkg>` (e.g. `from uc.oss import …`, with `uc` at `test/py/uc`).
- `scripts/<pkg>/` — the right call when the code is *also* useful to run standalone (a data generator a
  dev invokes by hand). It's test infra either way; keeping it here just preserves that ergonomics.

The one rule: **your import style must match the root you put on the path.** With `scripts/` on the path,
`import data_generators` works but `import scripts.data_generators` does *not* — the latter needs the repo
root (`.`) on the path instead. Pick one and set it once:

```ini
# pytest.ini — only when you need to change the default import roots
duckdb_pythonpath = test/py .        # → `from scripts.data_generators import …`  (repo root on path)
# or keep the default (scripts on the path) and import as `from data_generators import …`
```

A workable shape:

```
repo/
  pyproject.toml | requirements.txt    the one test env, at the root
  pytest.ini                           ducktest configure writes it; rootdir = here
  scripts/
    data_generators/                   fixture generators (also dev-runnable standalone)
    *.sh, mock servers                 test infra that isn't .py
  test/
    conftest.py                        suites (register_suite)
    py/<backend>/                      provisioners, service/credential code
    <suite dirs>/                      the .test / .py bodies
```

With both anchors in place the command is identical everywhere — the driver and every extension — a plain
`uv run pytest` (or `pytest` in an activated venv) from the repo root, resolving the same no matter how
it's launched. That invariance is the whole point: nothing about running the suite depends on remembering
how you're supposed to run it.

---

### Everyday use

ducktest follows pytest's conventions, so there's no new command to learn. You run `pytest` with its
usual flags:

```bash
pytest                       # the default set — your de-facto "smoke" run (bare; no -m needed)
pytest -m databricks         # a suite by its marker (the driver auto-applies suite markers)
pytest test/rest             # a path — directory or single file
pytest -k roundtrip          # substring / boolean-expr match on test names
pytest -n 8                  # parallelism (-n auto is the default from `ducktest configure`; -n0 serializes)
pytest --slow                # also collect .test_slow and .test_coverage bodies
pytest --slow -m slow        # run only those slow/coverage bodies
pytest -m 'databricks and not slow' -k attach -n 8   # combine freely
```

Bare `pytest` runs the default set, the union of the suites marked `default=True`, which serves as
your smoke run. There's no built-in `smoke` marker; the driver auto-applies markers named after your
suites, like `databricks`. The driver marks `.test_slow` and `.test_coverage` bodies as `slow` when
they are enabled with `--slow`. The wider vocabulary of `smoke`, `local`, `cloud`, and `all` is a
convention you opt into through suite names and marks. Making `-m smoke` work directly is on the
roadmap (PLAN.md § *Named test sets*).

---

### Integrating a backend (worked example — Iceberg)

This walks an extension from bare `.test` files up to tests that hit a live REST catalog needing
credentials and a docker service, with ducktest handling selection, credentials, container sharing,
and the xdist coordination underneath. It's a composite, built to show the whole surface in one place.
A real onboarding usually starts much smaller: the first Iceberg suite we brought up runs the
extension's own data generator through an embedded Spark session, with no catalog server and no
credentials. Each step below links to [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the details.

#### 1. A pure-SQLLogic test (no Python)

Drop a body under `test/`; it runs through the binary:

```
# test/iceberg/scan.test
require iceberg

statement ok
CREATE TABLE t AS SELECT * FROM iceberg_scan('...');

query I
SELECT count(*) FROM t;
----
42
```

```
pytest test/iceberg/scan.test        # one file
pytest                               # all .test under test/
```

That's the whole hello-world. Everything below is only for tests that need live resources.

#### 2. Declare your suites (`test/conftest.py`)

A **suite** is a named slice of the tests (by directory `path`) with a default-run policy and its
resources. Declare them in `test/conftest.py` (an *initial* conftest, so the driver sees them on the
controller up front):

```python
# test/conftest.py
def pytest_configure(config):
    # deferred imports: resolve after the driver has put test/py on sys.path
    from ducktest import register_suite, credential, service
    from iceberg_test.rest import (RestProvisioner, REST_SERVICE,
                                    load_token, token_ok, token_error, have_token)

    register_suite(config, "iceberg_local", path="test/iceberg", marker="iceberg",
                   default=True)                       # runs on a bare `pytest` (the smoke set)

    register_suite(config, "rest", path="test/rest", marker="rest",
                   default=False,                      # opt-in: NOT on a bare run
                   provisioner=RestProvisioner(config),
                   credentials=[credential("iceberg_rest_token",
                                           fetch=load_token, validate=token_ok,
                                           error=token_error, adopt="env",
                                           available=have_token)],
                   services=[REST_SERVICE])
```

You get the marker for free: the driver applies each suite's `marker` to every `.test`/`.py` under its
`path`, so `-m rest` and `-m 'not rest'` work. A bare `pytest` runs only the `default=True` suites and
prints a banner naming what it deselected. (ARCHITECTURE.md § *Selection*.)

#### 3. Credentials (the REST token)

Fetched once, up front, on the controller, then broadcast to workers. You supply four callables:

```python
# iceberg_test/rest.py
_VARS = ("ICEBERG_REST_URI", "ICEBERG_REST_TOKEN")

def load_token(config=None):            # fetch: env-wins, else your secret manager
    env = {k: os.environ[k] for k in _VARS if os.environ.get(k)}
    if all(k in env for k in _VARS):
        return env
    return {**_fetch_from_secret_store(), **env}   # e.g. `op read ...`

def token_ok(value):                    # validate: value-based (the fetched dict)
    return bool(value) and all(value.get(k) for k in _VARS)

def have_token():                       # available: NON-interactive env check (no fetch/op)
    return all(os.environ.get(k) for k in _VARS)

def token_error():                      # error: shown when creds are missing
    return "Iceberg REST creds unavailable — set ICEBERG_REST_URI/TOKEN or run the env script."
```

- `adopt="env"` makes the fetched dict available to the credential's own suite — the body's
  `${ICEBERG_REST_TOKEN}` substitutes, scoped to that suite; it is not a global `os.environ` merge.
- A test that's selected but can't be provisioned fails loud rather than skipping quietly.
- Collect-first resolves the final selection, including `-k`. `available()` avoids a fetch when ambient
  credentials are already usable; otherwise `fetch()` runs once through the shared store.
  (ARCHITECTURE.md § *Credentials*.)

#### 4. A docker service (the REST catalog container)

Provisioned during collect-first when a selected suite needs it, shared across all workers, and stopped
once by the controller. A fixture may consume the same service block:

```python
# iceberg_test/rest.py
from ducktest import service, provision_service

def _start(config):                     # boot it; return a JSON-able block
    port = _run_container()
    return {"uri": f"http://127.0.0.1:{port}", "port": port}

def _stop(config):                      # stop it (controller, at session end)
    _kill_container()

REST_SERVICE = service("iceberg-rest", start=_start, stop=_stop, fixture="iceberg_rest")

@pytest.fixture(scope="session")
def iceberg_rest(request):
    block = provision_service(request.config, REST_SERVICE)   # reads the shared service block
    return types.SimpleNamespace(**block)                     # tests read .uri / .port
```

The collect-first plan provisions the service even for a bare `.test` with no fixture. Store
single-flight ensures one managed instance; `to_env` publishes connection details to test subprocesses
and SQLLogic substitution.
A fixture consumer receives a stored startup failure without retrying the boot. For a bare `.test`, the
original provisioning error is currently suppressed and the test may instead fail because its service
environment is missing. See ARCHITECTURE.md § *Services* and docs/SERVICES.md.

#### 5. A Python-driven test (provision a table, run the body, assert)

Pair a `.py` **driver** with a same-stem `.test` **body**. The driver declares the tables it needs
(`@requires`); the `resources` fixture provisions them into an isolated schema; `run_paired` injects the
resulting env into the body:

```python
# test/rest/roundtrip.py           (driver — collected; runs the same-stem .test)
from ducktest import TableSpec, requires, run_paired

@requires(source=TableSpec("id_name").Seed(None), access="rw")   # a fresh isolated table
def test_roundtrip(request, iceberg_rest, resources):
    run_paired(request, env={**resources.env, "REST_URI": iceberg_rest.uri})
    # ... optional plain-Python assertions here too (the .py can assert, not just drive)
```

```
# test/rest/roundtrip.test          (body — SQLLogic; ${...} filled by resources.env)
statement ok
ATTACH '${CATALOG}' AS ice (TYPE iceberg, ...);

query I
SELECT count(*) FROM ice.${SCHEMA}.${TABLE};
----
3
```

The provisioner turns each `@requires` into a real table and returns its bindings as `resources.env`;
`access="rw"` gets an isolated per-test schema, so tests don't collide under xdist. You write a
provisioner by subclassing `ducktest.provision.Provisioner` and filling in a few hooks: how to run a
statement, how to name and build a table, what env to hand back. The loop over the specs and the
shared-vs-isolated bookkeeping come from the base class.

The `source` in a `@requires` doesn't have to be a `TableSpec`. It can be a table name, or a reference
type of your own that the provisioner knows how to instantiate. The Iceberg suite uses an `IcebergDef`
that points at an entry in the extension's generator registry, for example. `TableSpec` and a
backend-native ref like that share one contract (a lazy value the provisioner resolves at run time), but
differ in what they own and how portable they are; ARCHITECTURE.md § *Source refs* spells out the
contract and when to add your own.

The `${CATALOG}/${SCHEMA}/${TABLE}` names the body substitutes are a convention your provisioner produces
(reuse UC's `identity.py` or write your own); the `resources` fixture doesn't dictate the keys.
(ARCHITECTURE.md § *Provisioning*.)

#### 6. Run it

```
pytest                     # local/smoke: iceberg_local only; rest deselected + banner; no creds
pytest -m rest             # rest suite: token fetched up front (one prompt), container boots once
pytest test/rest           # same, by path
pytest -k roundtrip        # collect-first resolves -k; required resources provision up front
```

#### What you didn't have to write

You wrote two `register_suite` calls, four credential callables, a start/stop pair with a fixture, a
provisioner, and the tests. Everything else came from ducktest: selection and marking, the smoke
default and its banner, collect-first resource planning, single-flight provisioning, service sharing
and teardown, and xdist coordination. For how those work, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/INTERNALS.md](docs/INTERNALS.md).
