# ducktest

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

The design and internals are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (the model),
[docs/INTERNALS.md](docs/INTERNALS.md) (extending it), and [docs/PLAN.md](docs/PLAN.md) (the roadmap).

## Collecting Python-native tests

The first two shapes work with no setup. A plain pytest test (a `.py` with no `.test` beside it)
needs one config line: set `python_files = test_*.py` in your `pytest.ini` so pytest collects it.
That's off by default because duckdb core ships many non-test `test_*.py` scripts that would
otherwise get swept in; a clean extension repo just turns it on. Name these files `test_*.py` to keep
them apart from the `<stem>.py` drivers that pair with a `.test`. Once collected they behave like any
other test, so suites, credentials, and `@requires` all apply.

---

## Getting started

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
ducktest configure           # once: writes pytest.ini (testpaths, -n auto, importlib — a plugin
                             # can't inject these). Re-run to refresh; it won't clobber hand-edits.
pytest                       # auto-detects rootdir + test/, resolves build/<variant>/test/unittest,
                             # collects & runs every .test
```

Tools come from one build (`build/<KIND>/{test/unittest, duckdb}`), selected with `--build <KIND>` (or
`$BUILD_DIR`); override them individually with `--unittest-bin` / `--duckdb-bin`.

### Preset credentials in the environment

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

## Everyday use

ducktest follows pytest's conventions, so there's no new command to learn. You run `pytest` with its
usual flags:

```bash
pytest                       # the default set — your de-facto "smoke" run (bare; no -m needed)
pytest -m databricks         # a suite by its marker (the driver auto-applies suite markers)
pytest test/rest             # a path — directory or single file
pytest -k roundtrip          # substring / boolean-expr match on test names
pytest -n 8                  # parallelism (-n auto is the default from `ducktest configure`; -n0 serializes)
pytest -m 'databricks and not slow' -k attach -n 8   # combine freely
```

Bare `pytest` runs the default set, the union of the suites marked `default=True`, which serves as
your smoke run. There's no built-in `smoke` marker; the driver auto-applies markers named after your
suites, like `databricks`. The wider vocabulary of `smoke`, `local`, `cloud`, `slow`, and `all` is a
convention you opt into through how you name suites and mark tests. Making `-m smoke` work directly is
on the roadmap (PLAN.md § *Named test sets*).

---

## Integrating a backend (worked example — Iceberg)

This walks an extension from bare `.test` files up to tests that hit a live REST catalog needing
credentials and a docker service, with ducktest handling selection, credentials, container sharing,
and the xdist coordination underneath. It's a composite, built to show the whole surface in one place.
A real onboarding usually starts much smaller: the first Iceberg suite we brought up runs the
extension's own data generator through an embedded Spark session, with no catalog server and no
credentials. Each step below links to [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the details.

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

That's the whole hello-world. Everything below is only for tests that need live resources.

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

You get the marker for free: the driver applies each suite's `marker` to every `.test`/`.py` under its
`path`, so `-m rest` and `-m 'not rest'` work. A bare `pytest` runs only the `default=True` suites and
prints a banner naming what it deselected. (ARCHITECTURE.md § *Selection*.)

### 3. Credentials (the REST token)

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

- `adopt="env"` merges the fetched dict into `os.environ` so the body's `${ICEBERG_REST_TOKEN}` and any
  SDK see it.
- A test that's selected but can't be provisioned fails loud rather than skipping quietly.
- A predictable selection (`-m rest`, a path) fetches up front. A `-k`-selected rest test falls back to
  `available()`, then to a single-flighted late fetch (one prompt across workers, on by default).
  (ARCHITECTURE.md § *Credentials*.)

### 4. A docker service (the REST catalog container)

Provisioned lazily, on first need, shared across all workers, stopped once by the controller. Write a
start/stop pair plus a session fixture that routes through the store:

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

The first worker to pull `iceberg_rest` boots the container; the rest wait and reuse it. If the boot
fails, every waiter fails fast, with no retry. (ARCHITECTURE.md § *Services*.)

A `.test` file with no `.py` driver never pulls this fixture, so nothing would boot the service for it.
For a suite of bare `.test` bodies, bind the service eager instead, and ducktest boots it up front on
suite-selection and puts its connection details in the environment for the `.test` subprocess to read.
See ARCHITECTURE.md § *Services* and docs/SERVICES.md.

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

The provisioner turns each `@requires` into a real table and returns its bindings as `resources.env`;
`access="rw"` gets an isolated per-test schema, so tests don't collide under xdist. You write a
provisioner by subclassing `ducktest.provision.Provisioner` and filling in a few hooks: how to run a
statement, how to name and build a table, what env to hand back. The loop over the specs and the
shared-vs-isolated bookkeeping come from the base class.

The `source` in a `@requires` doesn't have to be a `Fixture`. It can be a table name, or a reference
type of your own that the provisioner knows how to instantiate. The Iceberg suite uses an `IcebergDef`
that points at an entry in the extension's generator registry, for example. `Fixture` and a
backend-native ref like that share one contract (a lazy value the provisioner resolves at run time), but
differ in what they own and how portable they are; ARCHITECTURE.md § *Source refs* spells out the
contract and when to add your own.

The `${CATALOG}/${SCHEMA}/${TABLE}` names the body substitutes are a convention your provisioner produces
(reuse UC's `identity.py` or write your own); the `resources` fixture doesn't dictate the keys.
(ARCHITECTURE.md § *Provisioning*.)

### 6. Run it

```
pytest                     # local/smoke: iceberg_local only; rest deselected + banner; no creds
pytest -m rest             # rest suite: token fetched up front (one prompt), container boots once
pytest test/rest           # same, by path
pytest -k roundtrip        # -k can't be predicted → token via available()/late-fetch, else fails loud
```

### What you didn't have to write

You wrote two `register_suite` calls, four credential callables, a start/stop pair with a fixture, a
provisioner, and the tests. Everything else came from ducktest: selection and marking, the smoke
default and its banner, fetching credentials once and broadcasting them to workers (with the backstop
and late fetch), the container's start-lock, sharing, and teardown, and the xdist coordination holding
it together. For how those work, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/INTERNALS.md](docs/INTERNALS.md).
