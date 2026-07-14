# ducktest

> **Naming:** the tool, CLI, and import are all **`ducktest`**. The published **distribution** stays
> **`duckdb-pytest-driver`** — so you `uv pip install … duckdb-pytest-driver` but `import ducktest` and
> run `ducktest` (the same split as `pip install pillow` → `import PIL`). If you had an older checkout
> installed, **reinstall** to pick up the renamed `ducktest` entry point.

A **pytest front-end for DuckDB, its extensions, and clients**, over the existing **`unittest` (Catch2)
binary**. It runs the binary's **SQLLogic `.test` files** (C++ `TEST_CASE`s are a planned lane) and adds
a Python layer: **first-class Python tests**, declarative `@requires` table provisioning, **suite**-based
selection, credential + service handling, and xdist parallelism.

The **`.test` file is the central artifact** — run alone, or paired with a same-stem `.py` sibling. And
**Python tests are first-class too**: a `.py` can carry its own assertions and skip SQLLogic where that
fits better — it's a real test, not thin glue.

- Auto-registered pytest plugin (a `pytest11` entry point) — no `pytest_plugins`, no `sys.path` hacks.
- Design & internals: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (the model),
  **[docs/INTERNALS.md](docs/INTERNALS.md)** (extending), **[docs/PLAN.md](docs/PLAN.md)** (roadmap).

## The three ways to write a test

- **`.test` alone** — SQLLogic, run through the binary. Zero Python. The default.
- **`.test` + `.py` sibling** — the `.py` provisions/decorates, runs the body via `run_paired`, and may
  add its own assertions.
- **`.py` (Python-native)** — own assertions, no SQLLogic. Set **`python_files = test_*.py`** in your
  `pytest.ini` to auto-collect them (it's off by default only to shield duckdb *core*, which has many
  non-test `test_*.py` scripts — a clean extension repo just turns it on). Name them `test_*.py` so
  they stay distinct from `<stem>.py` drivers. All suite / credential / `@requires` machinery applies
  unchanged — they're ordinary pytest tests.

---

## Getting started

pytest imports your test modules, so **pytest + its plugins (this driver) + all your test
dependencies must share one environment.** For any suite with real deps (pyspark, a SQL connector,
your own package), install them together:

```bash
# a venv holding EVERYTHING pytest needs to import
uv venv && source .venv/bin/activate
uv pip install -e /path/to/duckdb-pytest-driver pytest pytest-xdist \
    pyspark databricks-sql-connector <your test deps>
pytest

# or let uv build the env from your test project's declared deps
uv run --group test pytest        # `test` group = pytest, pytest-xdist, the driver, pyspark, …
```

**`uv tool install pytest` is dependency-free only.** It's handy for a pure-`.test` (or stdlib-only)
suite — but its environment is **isolated**, so it will **not** see test deps you install elsewhere.
Once your tests `import` a third-party package, use a venv/project as above.

```bash
# only for dependency-free suites (no imports beyond the stdlib):
uv tool install pytest --with pytest-xdist --with-editable /path/to/duckdb-pytest-driver
```

Then, in a **built** duckdb (or extension) checkout:

```bash
ducktest configure           # once: writes pytest.ini (testpaths, -n auto, importlib — a plugin
                             # can't inject these). Re-run to refresh; it won't clobber hand-edits.
pytest                       # auto-detects rootdir + test/, resolves build/<variant>/test/unittest,
                             # collects & runs every .test
```

Tools come from **one build** (`build/<KIND>/{test/unittest, duckdb}`), selected by `--build <KIND>`
(or `$BUILD_DIR`); override individually with `--unittest-bin` / `--duckdb-bin`.

### Keep runs fast and uninterrupted — preset creds in the env

For suites that need credentials, the framework fetches them **once, up front**, prompting your secret
tool (e.g. `op`) only for what's missing. To avoid *any* prompt — essential for `-n auto` and
benchmarks, where an interactive pause mid-run is fatal — **have the credentials already in the
environment**:

```bash
source your-databricks-env.sh && pytest -m databricks     # env pre-populated → no prompt
op run --env-file=creds.env -- pytest -m databricks       # or let your secret tool populate it
```

The framework's `available()` check sees them and skips the fetch. A run that *does* need to prompt does
so at invocation (never minutes in), and a genuinely missing credential **fails loud** — never a silent
skip.

## Everyday use (pytest conventions)

ducktest honors pytest's conventions — there's no new run command to learn; you run **`pytest`** and use
its flags:

```bash
pytest                       # the default set — your de-facto "smoke" run (bare; no -m needed)
pytest -m databricks         # a suite by its marker (the driver auto-applies suite markers)
pytest test/rest             # a path — directory or single file
pytest -k roundtrip          # substring / boolean-expr match on test names
pytest -n 8                  # parallelism (-n auto is the default from `ducktest configure`; -n0 serializes)
pytest -m 'databricks and not slow' -k attach -n 8   # combine freely
```

A **bare `pytest`** *is* the default set — the union of `default=True` suites — i.e. your de-facto
**smoke** run (there's no built-in `smoke` mark; the driver auto-applies *suite* markers like
`databricks`, not `smoke`). The standard vocabulary — `smoke · local · cloud · slow · all` — is a
**convention** you adopt by naming suites / marking tests; turning it into first-class presets (so
`-m smoke` "just works") is on the roadmap (PLAN.md § *Named test sets*).

---

## Integrating a backend (worked example — Iceberg)

Getting an extension's `.test` files + Python tests running, some against a live REST catalog that needs
**credentials** and a **docker service** — with the framework owning selection, credential handling,
container sharing, and xdist coordination. Concepts are one-lined here and linked to
**[ARCHITECTURE.md](docs/ARCHITECTURE.md)** for depth.

### 1. A pure-SQLLogic test (no Python)

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

That's the whole "hello world". Everything below is only for tests that need **live resources**.

### 2. Declare your suites (`test/conftest.py`)

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

For free: the driver auto-applies the suite's `marker` to every `.test`/`.py` under its `path`, so
`-m rest` / `-m 'not rest'` work; a **bare `pytest`** runs only `default=True` suites and prints a banner
naming what it deselected. (ARCHITECTURE.md § *Selection*.)

### 3. Credentials (the REST token)

Fetched **once, up front, on the controller**, then broadcast to workers. You supply four callables:

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

- `adopt="env"` merges the fetched dict into `os.environ` so the body's `${ICEBERG_REST_TOKEN}` and any
  SDK see it.
- A test **selected but unprovisionable fails loud** — never a silent skip. (The paradigm shift.)
- Predictable selections (`-m rest`, a path) fetch up front. A `-k`-selected rest test falls back to
  `available()`, then a single-flighted **late fetch** (one prompt across workers, default on).
  (ARCHITECTURE.md § *Credentials*.)

### 4. A docker service (the REST catalog container)

Provisioned **lazily, on first need**, shared across all workers, stopped once by the controller. Write a
start/stop plus a session fixture that routes through the store:

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
    block = provision_service(request.config, REST_SERVICE)   # first worker boots; rest share
    return types.SimpleNamespace(**block)                     # tests read .uri / .port
```

The first worker to pull `iceberg_rest` boots the container; the rest **block, then reuse** it. If the
boot fails, every waiter fails fast (no retry). (ARCHITECTURE.md § *Services*.)

### 5. A Python-driven test (provision a table, run the body, assert)

Pair a `.py` **driver** with a same-stem `.test` **body**. The driver declares the tables it needs
(`@requires`); the `resources` fixture provisions them into an isolated schema; `run_paired` injects the
resulting env into the body:

```python
# test/rest/roundtrip.py           (driver — collected; runs the same-stem .test)
from ducktest import Fixture, requires, run_paired

@requires(source=Fixture("id_name").Seed(None), access="rw")   # a fresh isolated table
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

The provisioner (registered on the suite) turns each `@requires` into a real table and returns its
bindings as `resources.env`; `access="rw"` gets an isolated per-test schema (no collisions under xdist).
The `${CATALOG}/${SCHEMA}/${TABLE}` vocabulary is a **convention you produce in your provisioner** (reuse
UC's `identity.py` or roll your own) — the driver's `resources` fixture is opaque about the env's keys;
it isn't yet driver-provided. (ARCHITECTURE.md § *Provisioning*.)

### 6. Run it

```
pytest                     # local/smoke: iceberg_local only; rest deselected + banner; no creds
pytest -m rest             # rest suite: token fetched up front (one prompt), container boots once
pytest test/rest           # same, by path
pytest -k roundtrip        # -k can't be predicted → token via available()/late-fetch, else fails loud
```

### What you did NOT write

Selection/marking, the smoke default + banner, the credential fetch-once-broadcast + backstop +
late-fetch, the container start-lock + sharing + teardown, and all the xdist coordination — those are
the driver's. You wrote two `register_suite` calls, four cred callables, a start/stop + fixture, a
provisioner, and your tests. The rest is **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** and
**[docs/INTERNALS.md](docs/INTERNALS.md)**.
