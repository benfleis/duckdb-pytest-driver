"""Generic pytest plugin / harness for duckdb test suites (the "driver" framework).

This is the pytest plugin, auto-registered via the ``pytest11`` entry point (see
pyproject.toml) — no ``pytest_plugins`` line and no ``sys.path`` hacks required. It owns
option registration, binary resolution, `.test` collection (the root-conftest logic folded
in here), the working-dir / test-root resolution, the per-run external-dir lifecycle, the
member/role/driver model + `run_paired`/`resources`, the `--repl` provisioning flow, and the
collection/run/report hooks. The SQLLogic `.test` *lane* (collecting and running `.test`
files through the binary) lives in `sqllogic`, which this module imports.

Import-name-agnostic on purpose (dual-mode): the same code works pip-installed as
``duckdb_pytest_driver`` OR vendored back into ``duckdb/test/py/``. Layout, model, and
consumer setup: see the repo README + docs/.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from .fixtures import duckdb_cli_for
from .mnemonic import run_id as _make_run_id
from .steps import step
from .sqllogic import (
    SqlLogicFile,
    SqlLogicItem,
    _invoke,
    _parse_result,
    _raise_for_result,
    resolve_unittest_args,
)


# ---------------------------------------------------------------------------
# Option registration
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Register all driver options + ini settings (auto-called by pytest).

    Folds in what the old consumer root-conftest used to do by hand: it called
    `register_options(parser)` from its own `pytest_addoption`. Now the plugin owns it, so a
    base `.test` consumer needs no conftest. `register_options` stays public (dual-mode /
    back-compat) but consumers no longer call it themselves.
    """
    register_options(parser)
    # Auto-detected defaults, overridable via ini or CLI. `sqllogic_working_dir` keeps its
    # attribute name for compatibility (run_paired + the CLI flow read it off `config`).
    parser.addini(
        "duckdb_working_dir",
        "Repo root the driver resolves build/<variant>/test/unittest and relative test "
        "names against. Default: pytest's rootdir. Overridden by --duckdb-working-dir.",
        default="",
    )
    parser.addini(
        "duckdb_test_root",
        "Directory tree walked for `.test` bodies + same-stem `.py` drivers. Default: "
        "<working_dir>/test. Overridden by --duckdb-test-root.",
        default="",
    )
    parser.addini(
        "duckdb_ignore_dirs",
        "Top-level directory names under working_dir to skip during collection "
        "(replaces the old `collect_ignore`). Default: duckdb build.",
        type="args",
        default=["duckdb", "build"],
    )
    parser.addini(
        "duckdb_pythonpath",
        "Repo-relative dirs prepended to sys.path so test-local helper packages import "
        "(e.g. a backend conftest's `from uc.oss import ...`, where `uc` lives at test/py/uc). "
        "Mirrors the old root-conftest sys.path inserts; each added only if it exists. "
        "Default: test/py scripts scripts/data_generator.",
        type="args",
        default=["test/py", "scripts", "scripts/data_generator"],
    )
    parser.addoption(
        "--duckdb-working-dir",
        default=None,
        metavar="DIR",
        help="Override the repo root (else ini duckdb_working_dir, else pytest rootdir).",
    )
    parser.addoption(
        "--duckdb-test-root",
        default=None,
        metavar="DIR",
        help="Override the test tree root (else ini duckdb_test_root, else <working_dir>/test).",
    )


def register_options(parser):
    parser.addoption(
        "--build",
        default="auto",
        choices=["auto", "debug", "release", "reldebug", "relassert", "latest"],
        help="Which build's test tools to use (default: auto). The tools are a "
        "PRECONDITION for testing; both come from ONE build: debug/release/reldebug/"
        "relassert resolve to build/{type}/{test/unittest, duckdb} under the repo root. "
        "auto = the single built variant among (debug, release, reldebug, relassert), "
        "erroring if none or more than one. latest = the most-recently-built. Overridden "
        "by $BUILD_DIR and the per-tool --unittest-bin / --duckdb-bin.",
    )
    parser.addoption(
        "--unittest-binary",
        "--unittest-bin",
        default=None,
        metavar="PATH",
        help="Explicit path to the unittest (Catch2-compatible) test binary. "
        "Overrides --build and $BUILD_DIR. Use this when the binary "
        "lives outside the standard CMake build tree.",
    )
    parser.addoption(
        "--duckdb-bin",
        "--duckdb-binary",
        default=None,
        metavar="PATH",
        help="Explicit path to the duckdb CLI (used to instantiate table fixtures via "
        "the middleman, and by --repl). Overrides --build and $BUILD_DIR. Default: the "
        "`duckdb` next to the resolved unittest binary (build/<variant>/duckdb).",
    )
    parser.addoption(
        "--batch-size",
        default=10,
        type=int,
        metavar="N",
        help="Tests per unittest invocation (default: 10). Reduces subprocess "
        "overhead; use with -n for parallel batches.",
    )
    parser.addoption(
        "--unittest-args",
        action="append",
        default=[],
        metavar="ARGS",
        help="Extra argument(s) appended verbatim to EVERY unittest binary invocation "
        "(both the bare-.test collector lane and run_paired). Repeatable; each value is "
        "shlex-split, so --unittest-args='--test-config x.json' adds two tokens. Generic "
        "passthrough for binary flags the driver does not model directly (e.g. "
        "--test-config, --skip-error-messages). A consuming repo sets a default via its "
        "own (non-symlinked) conftest, since pytest.ini is shared SoT.",
    )
    parser.addoption(
        "--temp-dir-base",
        default=None,
        metavar="BASE",
        help="Caller-owned base dir for this run's temp dirs. Tests run under "
        "BASE/<run-id>/<test> (run-id = timestamp--mnemonic, one per pytest run, shared "
        "across xdist workers; <test> = the binary's per-test subdir). pytest owns "
        "BASE/<run-id>: the binary is invoked with explicit --temp-dir-base BASE/<run-id> "
        "--run-id <run-id> --temp-dir-run-id off --temp-dir-destroy never, so RUN_ID matches "
        "pytest's and it places per-test subdirs but never deletes them. Disposition of "
        "BASE/<run-id> is controlled by "
        "--temp-dir-destroy. BASE may be local or remote (e.g. s3://).",
    )
    parser.addoption(
        "--temp-dir-destroy",
        default="on-success",
        choices=["never", "on-success", "always"],
        metavar="{never,on-success,always}",
        help="Destroy disposition for the per-run dir (BASE/<run-id>), applied pytest-side "
        "at sessionfinish: never (keep) | on-success (default — remove only when the run "
        "has no failures) | always (remove regardless). The binary is always passed "
        "--temp-dir-destroy never for BASE/<run-id> (pytest owns that level); this governs "
        "the pytest-side cleanup.",
    )
    # --- @requires-driven provisioning / interactive shell ------------------
    parser.addoption(
        "--repl",
        action="store_true",
        default=False,
        help="For the SINGLE selected test, read its @requires, provision its "
        "fixtures via the extension provisioner, and drop into an interactive shell "
        "attached to them. The shell follows the test's body (repl-kind = body-kind): "
        "a SQL body -> duckdb CLI (current); a pure-.py body -> python shell (planned). "
        "Tears down on exit (unless --provision-keep). Requires exactly one selected "
        "test carrying @requires.",
    )
    parser.addoption(
        "--provision-keep",
        action="store_true",
        default=False,
        help="With --repl: do NOT tear down provisioned fixtures on exit; print the "
        "teardown command instead so they can be reused / cleaned up later.",
    )
    parser.addoption(
        "--provision-dry-run",
        action="store_true",
        default=False,
        help="With --repl: print the resolved @requires specs, the provision plan "
        "(cell schema + per-table commands) and the would-be duckdb init SQL, then "
        "stop. Performs NO DDL, does NOT launch the CLI, does NOT tear down. "
        "Dominates --provision-keep.",
    )
    parser.addoption(
        "--steps",
        action="store_true",
        default=False,
        help="Surface driver step() messages live (provisioning, clones, teardown, "
        "service startup, with timings). Focus alias: turns on live-log at INFO and "
        "raises the `driver` logger to INFO (root stays WARNING, so third-party INFO "
        "chatter stays quiet). Plain -v does NOT show these (pytest captures terminal "
        "writes during a test). Forces single-process (-n0), since live-log is off on "
        "xdist workers. --repl / --provision-keep auto-enable this (an interactive session "
        "narrates its provision/teardown); pass --no-steps to opt back out.",
    )
    parser.addoption(
        "--no-steps",
        action="store_true",
        default=False,
        help="Suppress the step() narration that --repl / --provision-keep auto-enable — a "
        "quiet interactive session. No effect on its own (and --steps still wins if both "
        "are passed).",
    )


# Concrete CMake build variants scanned by `--build auto` / `--build latest`. Each
# resolves to build/<variant>/test/unittest under the repo root.
BUILD_VARIANTS = ("debug", "release", "reldebug", "relassert")


def _variant_binary(working_dir, variant):
    return os.path.join(working_dir, "build", variant, "test", "unittest")


def _built_variants(working_dir):
    """(variant, path, mtime) for each BUILD_VARIANTS whose unittest binary exists."""
    out = []
    for v in BUILD_VARIANTS:
        p = _variant_binary(working_dir, v)
        if os.path.isfile(p):
            out.append((v, p, os.path.getmtime(p)))
    return out


def _variants_listing():
    return ", ".join("build/%s/test/unittest" % v for v in BUILD_VARIANTS)


def find_binary(config, working_dir):
    """Return the unittest binary path.

    Resolution priority: --unittest-binary > $BUILD_DIR > --build. For an explicit
    build variant the path is returned even if it doesn't exist (callers check
    os.path.exists for a friendly message); --build auto/latest inspect the built
    variants and raise pytest.UsageError when they can't pick exactly one.
    """
    explicit = config.getoption("--unittest-binary", default=None)
    if explicit:
        return os.path.abspath(explicit)

    build_dir = os.environ.get("BUILD_DIR")
    if build_dir:
        return os.path.join(build_dir, "test", "unittest")

    build = config.getoption("--build", default="auto")

    if build == "auto":
        found = _built_variants(working_dir)
        if len(found) == 1:
            return found[0][1]
        if not found:
            raise pytest.UsageError(
                f"--build auto: no unittest binary in any of {_variants_listing()} "
                f"(under {working_dir}). Build one, or pass --build <variant>, "
                "--unittest-binary PATH, or set $BUILD_DIR."
            )
        raise pytest.UsageError(
            "--build auto: multiple unittest binaries built (%s); ambiguous. Pass "
            "--build <variant>, --build latest, or --unittest-binary PATH." % ", ".join(v for v, _, _ in found)
        )

    if build == "latest":
        found = _built_variants(working_dir)
        if not found:
            raise pytest.UsageError(
                f"--build latest: no unittest binary in any of {_variants_listing()} " f"(under {working_dir})."
            )
        return max(found, key=lambda t: t[2])[1]

    # explicit variant (debug/release/reldebug/relassert)
    return _variant_binary(working_dir, build)


def find_duckdb(config, working_dir):
    """Return the duckdb CLI path — the fixture-instantiation / --repl tool.

    Symmetric to find_binary but for the `duckdb` leaf, so both tools resolve from one
    build: --duckdb-bin > $BUILD_DIR ($BUILD_DIR/duckdb) > the `duckdb` next to the
    resolved unittest binary (build/<variant>/duckdb). Presence is a PRECONDITION: raises
    pytest.UsageError, naming the two ways to satisfy it, when the CLI isn't there.
    """
    explicit = config.getoption("--duckdb-bin", default=None)
    if explicit:
        path = os.path.abspath(explicit)
    else:
        build_dir = os.environ.get("BUILD_DIR")
        path = (
            os.path.join(build_dir, "duckdb")
            if build_dir
            else duckdb_cli_for(find_binary(config, working_dir))
        )
    if not os.path.isfile(path):
        raise pytest.UsageError(
            f"duckdb CLI not found at {path}. The test tools are a precondition: build "
            "build/<variant>/duckdb (selected by --build / $BUILD_DIR, alongside the "
            "unittest binary), or pass --duckdb-bin PATH."
        )
    return path


def pytest_report_header(config):
    """Verbose-mode (`-v`) trace of the resolved test tools — which build was picked.

    Prints in the session header (next to rootdir/plugins) so a run's tool provenance
    (e.g. build/relassert/{test/unittest, duckdb}) is visible. Best-effort: a tool that
    can't be resolved yet is simply omitted (never breaks the header).
    """
    if int(config.getoption("verbose", default=0) or 0) < 1:
        return None
    working_dir = getattr(config, "sqllogic_working_dir", None) or os.getcwd()
    lines = []
    for label, resolve in (("unittest", find_binary), ("duckdb", find_duckdb)):
        try:
            lines.append(f"duckdb-pytest-driver {label}: {resolve(config, working_dir)}")
        except Exception:
            pass  # not resolvable yet (e.g. no binary for a pure-collection run) — skip
    return lines or None


# ---------------------------------------------------------------------------
# Working dir / test root resolution + collection
#
# Folded in from the old consumer root-conftest, which hardcoded these to its own
# directory. Now they auto-detect (rootdir / <rootdir>/test) and are overridable via
# ini (`duckdb_working_dir`, `duckdb_test_root`, `duckdb_ignore_dirs`) or CLI, so a base
# `.test` consumer needs zero conftest. `config.sqllogic_working_dir` keeps its name.
# ---------------------------------------------------------------------------


def _resolve_working_dir(config):
    cli = config.getoption("--duckdb-working-dir", default=None)
    if cli:
        return os.path.abspath(cli)
    ini = config.getini("duckdb_working_dir")
    if ini:
        return os.path.abspath(ini)
    # pytest's auto-detected rootdir — the repo root in the common `cd <checkout>; pytest` case.
    return str(config.rootpath)


def _working_dir(config):
    """The resolved working dir, cached on config as `sqllogic_working_dir`."""
    wd = getattr(config, "sqllogic_working_dir", None)
    if wd is None:
        wd = _resolve_working_dir(config)
        config.sqllogic_working_dir = wd
    return wd


def _test_root(config):
    cli = config.getoption("--duckdb-test-root", default=None)
    if cli:
        return os.path.abspath(cli)
    ini = config.getini("duckdb_test_root")
    if ini:
        return os.path.abspath(ini)
    return os.path.join(_working_dir(config), "test")


def _ensure_pythonpath(config):
    """Prepend the configured test-local import roots (`duckdb_pythonpath`) to sys.path.

    Restores the old root-conftest's sys.path inserts so a backend conftest's
    `from uc.oss import ...` resolves. Idempotent; each dir added only if it exists.
    """
    working_dir = _working_dir(config)
    for rel in config.getini("duckdb_pythonpath"):
        p = os.path.join(working_dir, rel)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


@pytest.hookimpl(hookwrapper=True)
def pytest_load_initial_conftests(early_config, parser, args):
    # Prepend the test-local import roots BEFORE any initial conftest is imported. An explicit
    # path arg (`pytest test/sql/databricks/foo.py`) makes that dir's conftest an *initial*
    # conftest, loaded here — before pytest_configure — so its `import uc` needs the path set
    # now. hookwrapper: our pre-yield runs before pytest actually loads those conftests. (Bare
    # invocation loads them during collection, after configure — pytest_configure covers that.)
    _ensure_pythonpath(early_config)
    yield


def pytest_ignore_collect(collection_path, config):
    """Skip the configured top-level dirs under working_dir (was `collect_ignore`).

    A plugin can't set the `collect_ignore` module var, so the ignore list moves to this
    hook. Matches exact `<working_dir>/<name>` entries (default: duckdb/, build/) — the same
    semantics the root-conftest's `collect_ignore = ["duckdb","build"]` had.
    """
    working_dir = _working_dir(config)
    target = str(collection_path)
    for name in config.getini("duckdb_ignore_dirs"):
        if target == os.path.join(working_dir, name):
            return True
    return None


def pytest_collect_file(parent, file_path):
    """Collect `.test` bodies and same-stem `.py` drivers under the test root (was in conftest).

    - a driver `.py` (has a same-stem `.test`) → a normal pytest Module, so its fixtures wrap
      the body with Python initialize/finalize;
    - a `.test` WITH a same-stem `.py` driver → suppressed (runs only via that driver);
    - a driverless `.test` → a SqlLogicFile that runs it through the binary.
    Everything outside the test root is ignored so pytest stays out of scripts/, tools/, etc.
    """
    config = parent.config
    working_dir = _working_dir(config)
    test_root = _test_root(config)
    if not str(file_path).startswith(test_root + os.sep):
        return None
    if file_path.suffix == ".py":
        if is_driver(file_path):
            return pytest.Module.from_parent(parent, path=file_path)
        return None
    # `.sql` bodies + `.test_slow`/`.test_coverage` are PLANNED body types (see docs/PLAN.md,
    # the scan-reconcile sprint) — only `.test` is collected today; extend this gate then.
    if file_path.suffix != ".test":
        return None
    if has_driver(file_path):
        return None
    binary = find_binary(config, working_dir)
    return SqlLogicFile.from_parent(parent, path=file_path, binary=binary, working_dir=working_dir)


# ---------------------------------------------------------------------------
# Per-run temp dir: pytest owns BASE/<run-id> + cleanup
#
# When --temp-dir-base BASE is given, tests run under BASE/<run-id>/<test>:
#   <run-id> = timestamp--mnemonic, ONE per pytest run, computed on the controller
#              and shared to xdist workers so every worker writes under the same
#              BASE/<run-id>; <test> = the binary's per-test subdir.
# pytest owns BASE/<run-id>: the binary is invoked with --temp-dir-run-id off +
# --temp-dir-destroy never, and pytest removes BASE/<run-id> here on a clean run.
# ---------------------------------------------------------------------------


def _run_id(config):
    """Return this run's id, cached on config; shared across xdist workers."""
    cached = getattr(config, "_sqllogic_run_id", None)
    if cached is not None:
        return cached
    wi = getattr(config, "workerinput", None)
    run_id = wi["sqllogic_run_id"] if wi and "sqllogic_run_id" in wi else _make_run_id()
    config._sqllogic_run_id = run_id
    return run_id


def _run_dir(config):
    """BASE/<run-id> for this run, or None if --temp-dir-base was not given."""
    base = config.getoption("--temp-dir-base", default=None)
    return os.path.join(base, _run_id(config)) if base else None


def pytest_configure_node(node):
    # xdist controller hook: hand each worker the controller's run-id so all
    # workers share one BASE/<run-id>.
    node.workerinput["sqllogic_run_id"] = _run_id(node.config)


def pytest_sessionfinish(session, exitstatus):
    # Controller-only: drop the per-run external dir on a clean run (no failures),
    # unless asked to keep it. Always kept on failure/interruption for debugging.
    config = session.config
    if getattr(config, "workerinput", None) is not None:
        return  # this is a worker
    destroy = config.getoption("--temp-dir-destroy", default="on-success")
    if destroy == "never":
        return
    if destroy == "on-success" and int(exitstatus) != 0:
        return  # keep on failure/interruption for debugging
    run_dir = _run_dir(config)
    if run_dir and os.path.isdir(run_dir):  # isdir() also skips remote (s3://) bases
        shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Members, roles, and drivers
#
# A *test* is the set of same-stem *members* under test/** (e.g. foo.test +
# foo.py). Each member plays one or more *roles*:
#   - body   : holds test logic that is run — a `.test` (`.sql` is PLANNED, not yet a
#              collected body type), or a `.py` that carries its own assertions (the
#              py-exclusive case).
#   - driver : orchestrates execution (initialize/finalize) around a body — a `.py`.
# Roles are not file types: a `.py` can be a driver, a body, or BOTH (drive a
# same-stem body AND carry its own assertions). A `.test` is always a body (`.sql` planned).
#
# This module handles the *driver* case: a `.py` with a same-stem `.test` body.
# The `.py` is collected and drives that body through the binary via run_paired();
# the standalone body is suppressed (see conftest) so it runs only via its driver.
#
# CURRENT LIMITATION: the driving `.py` is the reported unit, not the body —
# flipping so the body is reported (driver only contributes hooks) is the pending
# rework. The py-as-body-only case (a `.py` with no same-stem body) is the
# deferred py-exclusive lane — not collected today (python_files is off).
# ---------------------------------------------------------------------------


def _stem_path(path, suffix):
    return os.path.splitext(str(path))[0] + suffix


def has_driver(body_path) -> bool:
    """True if this body (`.test`/`.sql`) has a same-stem `.py` driver."""
    return os.path.exists(_stem_path(body_path, ".py"))


def is_driver(py_path) -> bool:
    """True if this `.py` is a driver — it has a same-stem body (`.test`) to drive.

    A `.py` with no same-stem body is itself a body (the py-exclusive case), not a
    driver; that lane is not handled here yet.
    """
    return os.path.exists(_stem_path(py_path, ".test"))


def run_paired(request, *, temp_dir_base=None, env=None):
    """Drive the calling driver `.py`'s same-stem body (`.test`) through the binary.

    Call from the driver's test function once initialization has run. Raises
    SqlLogicFailure on failure and pytest.skip on a skipped test, so the driver
    reflects the real SQL result. Pass temp_dir_base to root the binary's temp dir at a
    caller-owned base (BASE/<run-id>) that initialization already staged into; the binary
    then gets explicit --temp-dir-base/--temp-dir-run-id off/--temp-dir-destroy never.
    Pass env (e.g. a provisioning fixture's bindings.env) to inject vars the body
    substitutes via ${...} (merged over os.environ).
    """
    working_dir = request.config.sqllogic_working_dir
    binary = find_binary(request.config, working_dir)
    test_path = _stem_path(request.path, ".test")
    test_name = os.path.relpath(test_path, working_dir)
    with step(f"running {test_name}"):
        _raise_for_result(
            _parse_result(
                _invoke(
                    binary,
                    [test_name],
                    working_dir,
                    temp_dir_base=temp_dir_base,
                    env=env,
                    extra_args=resolve_unittest_args(request.config),
                )
            ),
            test_file=str(test_path),
        )


class _EmptyBindings:
    """Yielded by `resources` for a test with no @requires (harmless empty env)."""

    env: dict = {}


@pytest.fixture
def matrix_cell(request):
    """The current `@requires_matrix` cell (its concrete properties dict); `None` when the
    test is not a matrix test.

    `@requires_matrix` emits `parametrize("matrix_cell", …, indirect=True)`, so the cell
    value routes THROUGH this fixture (the body has no `matrix_cell` argument). `resources`
    depends on it purely to pull it into every matrix test's fixture closure — indirect
    parametrize requires the fixture to be reachable from the item.
    """
    return getattr(request, "param", None)


@pytest.fixture
def resources(request, matrix_cell):  # matrix_cell: closure hook for indirect @requires_matrix (unused here)
    """Provision a test's @requires fixtures, yield the bindings, tear down after.

    The generic run-path counterpart of --repl (same provisioner): a driver does

        def test_x(request, resources):
            run_paired(request, env=resources.env)

    and the body's ${UC_TEST_CATALOG}/${UC_TEST_SCHEMA} substitute to the provisioned
    values. Per-test token, so cell schemas don't collide across tests/workers.
    No-ops (empty env) for tests without @requires.
    """
    from .provision import get_provisioner
    from .requires import collect_requirements

    specs = collect_requirements(request.node)
    if not specs:
        yield _EmptyBindings()
        return
    provisioner = get_provisioner(request.config, request.node.path)
    if provisioner is None:
        raise pytest.UsageError(
            f"{request.node.nodeid}: @requires present but no provisioner registered "
            "(the backend conftest must call driver.register_provisioner)."
        )
    token = _provision_token(request.config, request.node)
    bindings = provisioner.provision(specs, token, params=_item_params(request.node))
    try:
        yield bindings
    finally:
        provisioner.teardown(token, bindings=bindings)


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Resolve + cache the working dir (rootdir / ini / CLI) up front. run_paired() and the
    # --repl flow read `config.sqllogic_working_dir`; the collection hooks derive test_root
    # from it. Folded in from the old consumer root-conftest (which hardcoded it).
    # Resolve + cache the working dir (run_paired / --repl read config.sqllogic_working_dir), then
    # (re)assert the test-local import roots as a backstop for the bare-invocation path — explicit
    # path args were handled earlier in pytest_load_initial_conftests. Idempotent.
    _working_dir(config)
    _ensure_pythonpath(config)

    # Register the @requires marker so it doesn't warn under --strict-markers and
    # shows up in `pytest --markers`.
    config.addinivalue_line(
        "markers",
        "requires(source, access, properties, name): declare an external "
        "resource need; drives provisioning under --repl (see driver/requires.py).",
    )

    # Selecting a paired body (`foo.test`) by name collects nothing — it's suppressed
    # in favor of its same-stem driver (`foo.py`). Redirect an explicitly-named
    # `.test` arg to its driver so naming EITHER member runs the one test. Broad/dir
    # runs are unaffected (the suppression still prevents double-collection there).
    rewritten = []
    for arg in config.args:
        if arg.endswith(".test") and os.path.exists(arg):
            driver = arg[: -len(".test")] + ".py"
            if os.path.exists(driver):
                rewritten.append(driver)
                continue
        rewritten.append(arg)
    config.args[:] = rewritten

    # Both --repl and --steps must run single-process; force it here so neither silently
    # no-ops when addopts defaults to `-n auto` (the common case). --repl: under xdist the
    # controller never holds the collected items (workers do), so the flow sees 0 items
    # ("no tests ran"). --steps: live-log (its only channel) is disabled on xdist workers,
    # so steps would never print. tryfirst so this lands before pytest-xdist reads
    # numprocesses.
    if config.getoption("--repl", default=False) or config.getoption(
        "--steps", default=False
    ):
        if getattr(config.option, "numprocesses", None):
            config.option.numprocesses = 0
        if getattr(config.option, "dist", "no") not in (None, "no"):
            config.option.dist = "no"

    # Always emit driver step() records at INFO so they're CAPTURED and appear in the
    # "Captured log" section of a failing test's report — including under xdist, where
    # the report (with its captured logs) is pickled back to the controller even though
    # live worker output can't be (see pytest-xdist "known limitations": that's about
    # -s real-time streaming, not captured-on-report sections). Passing tests print
    # nothing (captured sections show only on failure), so this is free diagnostics with
    # no added noise; root stays at WARNING, so third-party INFO is still suppressed.
    logging.getLogger("driver").setLevel(logging.INFO)

    # Surface those step() logs LIVE (not just on failure) when narrating: --steps asks for it
    # explicitly, and --repl / --provision-keep auto-enable it (an interactive session should
    # narrate its own provision/teardown) unless --no-steps opts out. Setting --log-cli-level
    # turns on pytest's live-log — the only channel that cooperates with output capture, and it
    # also reaches the collection hook --repl runs from; it's dead on xdist workers, so this path
    # forces -n0 (above). Don't override an explicit --log-cli-level the user already passed.
    repl_like = config.getoption("--repl", default=False) or config.getoption("--provision-keep", default=False)
    narrate = config.getoption("--steps", default=False) or (
        repl_like and not config.getoption("--no-steps", default=False)
    )
    if narrate:
        if config.getoption("--log-cli-level", default=None) is None:
            config.option.log_cli_level = "INFO"


# ---------------------------------------------------------------------------
# --repl: @requires-driven provision → interactive duckdb → teardown
#
# Runs at end of collection (before the run loop). It picks the SINGLE selected
# test, reads its @requires, and hands the specs to the extension provisioner.
# Then it ALWAYS pytest.exit()s — --repl never runs the normal SQL body (that
# run path, run_paired with injected env, is the deferred follow-on). Precedence:
# --provision-dry-run dominates --provision-keep.
# ---------------------------------------------------------------------------


def pytest_collection_finish(session):
    config = session.config
    if not config.getoption("--repl", default=False):
        return
    # xdist workers also reach here; only the controller should drive the CLI.
    if getattr(config, "workerinput", None) is not None:
        return
    _cli_provision_flow(session, config)


def _item_params(item):
    """A parametrized item's params dict ({} if not parametrized).

    Lets --repl / the resources fixture convey a test's parametrization (e.g. a `schema`
    param) to the provisioner, so a single REPL context can match the SELECTED param
    (`--repl -k 'test[external]'` lands in external, not the default). The provisioner
    decides which param keys it understands; the framework just forwards them.
    """
    cs = getattr(item, "callspec", None)
    return dict(cs.params) if cs is not None else {}


def _cli_provision_flow(session, config):
    from .requires import collect_requirements
    from .provision import get_provisioner

    dry_run = config.getoption("--provision-dry-run", default=False)
    keep = config.getoption("--provision-keep", default=False)

    items = list(session.items)
    if len(items) != 1:
        raise pytest.UsageError(
            f"--repl requires exactly ONE selected test, but {len(items)} were "
            "collected. Narrow the selection (pass a single driver .py / use -k)."
        )
    item = items[0]
    specs = collect_requirements(item)
    provisioner = get_provisioner(config, item.path)

    # @requires is NOT needed just to get a prompt. With no provisioner we still launch
    # a bare duckdb REPL for ANY test (you LOAD/ATTACH by hand). The only hard error is a
    # test that DECLARES @requires when nothing is registered to satisfy them.
    if provisioner is None:
        if specs:
            raise pytest.UsageError(
                f"--repl: {item.nodeid!r} declares @requires but no provisioner is "
                "registered to satisfy them (the backend conftest must call "
                "driver.register_provisioner(...)). Remove @requires for a bare REPL."
            )
        print()
        print("=" * 70)
        print(f"--repl for test: {item.nodeid}")
        print("no @requires + no provisioner -> launching a bare duckdb REPL")
        print("=" * 70)
        if dry_run:
            pytest.exit(
                "--repl --provision-dry-run: nothing to provision; would launch a bare REPL",
                returncode=0,
            )
        _launch_cli(config, "")
        pytest.exit("--repl session complete", returncode=0)
        return

    token = _provision_token(config, item)

    print()
    print("=" * 70)
    print(f"--repl for test: {item.nodeid}")
    print(f"provision token: {token}")
    print("-" * 70)
    if specs:
        print("resolved @requires specs:")
        for i, s in enumerate(specs):
            print(
                f"  [{i}] source={s.source} access={s.access} "
                f"properties={s.properties} name={s.resolved_name()}"
            )
    else:
        print("no @requires -> minimal provision (REPL only)")
    print("=" * 70)

    if dry_run:
        # Plan only — NO DDL, NO launch, NO teardown. The provisioner prints its
        # plan (cell schema + per-table commands); we then print the init SQL.
        bindings = provisioner.provision(specs, token, dry_run=True, params=_item_params(item))
        print()
        print("----- would-be duckdb init SQL (secrets redacted) -----")
        print(provisioner.make_init(bindings, redact=True))
        print("-------------------------------------------------------")
        print()
        print("--provision-dry-run: NO DDL executed, CLI NOT launched, NO teardown.")
        pytest.exit("--repl --provision-dry-run complete", returncode=0)
        return

    bindings = provisioner.provision(specs, token, dry_run=False, params=_item_params(item))
    try:
        init_sql = provisioner.make_init(bindings)
        _launch_cli(config, init_sql)
    finally:
        if keep:
            print()
            print(f"--provision-keep: leaving fixtures (token={token}) in place.")
            print("Tear them down later via the backend's clean tool / teardown(token).")
        else:
            with step(f"tearing down provisioned fixtures (token={token})"):
                provisioner.teardown(token, bindings=bindings)
    pytest.exit("--repl session complete", returncode=0)


def _provision_token(config, node=None):
    """SQL-safe per-invocation id for cell-schema names.

    Run-id is `timestamp--mnemonic`; we keep the mnemonic, SQL-safe (dashes →
    underscores). When `node` is given, a short nodeid hash is appended so each test
    gets a UNIQUE token — parallel-safe (no two tests share a cell schema) and stable
    across workers (run-id is broadcast; nodeid differs per test).
    """
    rid = _run_id(config)
    mnem = rid.split("--", 1)[-1].replace("-", "_")
    if node is None:
        return mnem
    import hashlib

    return f"{mnem}_{hashlib.sha1(node.nodeid.encode()).hexdigest()[:6]}"


def _launch_cli(config, init_sql):
    """Write init_sql to a temp file and exec an interactive `duckdb -unsigned -init`.

    The duckdb binary lives next to the unittest binary's build dir; we derive it
    from the same build resolution. -unsigned is required to LOAD locally-built
    extensions (cannot be SET from inside the init file once the DB is running).
    """
    working_dir = getattr(config, "sqllogic_working_dir", os.getcwd())
    binary = find_binary(config, working_dir)
    # binary is <build>/test/unittest → duckdb CLI is <build>/duckdb
    build_dir = os.path.dirname(os.path.dirname(binary))
    duckdb_bin = os.path.join(build_dir, "duckdb")
    if not os.path.isfile(duckdb_bin):
        raise pytest.UsageError(
            f"--repl: duckdb CLI not found at {duckdb_bin}. Build it (e.g. make release)."
        )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", prefix="cli_init.", delete=False) as f:
        f.write(init_sql)
        init_path = f.name
    # Give duckdb a real interactive terminal. Two parts, both needed:
    #  1) suspend pytest's capture (so it isn't holding the std fds), and
    #  2) wire the subprocess to the CONTROLLING terminal explicitly via /dev/tty.
    # Capture-suspension alone left duckdb with a non-tty stdin → it ran the init,
    # hit EOF and exited with no prompt. /dev/tty is the real fd regardless of how
    # this process's 0/1/2 were redirected.
    capman = config.pluginmanager.getplugin("capturemanager")
    cmd = [duckdb_bin, "-unsigned", "-init", init_path]

    def _run():
        try:
            tty = open("/dev/tty", "r+b", buffering=0)
        except OSError:
            # No controlling terminal (e.g. CI) — nothing to be interactive against.
            subprocess.run(cmd, cwd=working_dir)
            return
        try:
            subprocess.run(cmd, cwd=working_dir, stdin=tty, stdout=tty, stderr=tty)
        finally:
            tty.close()

    try:
        with step("launching interactive duckdb CLI (exit to continue)"):
            if capman is not None:
                with capman.global_and_fixture_disabled():
                    _run()
            else:
                _run()
    finally:
        os.unlink(init_path)


# ---------------------------------------------------------------------------
# Post-collection hook: assign batch IDs and xdist_group markers
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(session, config, items):
    # Dedupe by nodeid: a driver .py named explicitly on the CLI is collected
    # both natively and by our hook, and (later) a .test reachable via both the
    # filesystem scan and `unittest -l` overlaps. Keep the first of each.
    seen = set()
    deduped = []
    for it in items:
        if it.nodeid in seen:
            continue
        seen.add(it.nodeid)
        deduped.append(it)
    items[:] = deduped

    batch_size = config.getoption("--batch-size", default=10)
    if batch_size <= 1:
        return

    batch_id = 0
    i = 0
    while i < len(items):
        item = items[i]
        if not isinstance(item, SqlLogicItem):
            i += 1
            continue

        batch: list[SqlLogicItem] = []
        while (
            i < len(items)
            and isinstance(items[i], SqlLogicItem)
            and items[i]._binary == item._binary
            and items[i]._working_dir == item._working_dir
            and len(batch) < batch_size
        ):
            batch.append(items[i])
            i += 1

        test_names = [b._test_name for b in batch]
        for b in batch:
            b._batch_id = batch_id
            b._batch_test_names = test_names
            b.add_marker(pytest.mark.xdist_group(f"sqllogic_batch_{batch_id}"))

        batch_id += 1


# ---------------------------------------------------------------------------
# Terminal summary: aggregate skip reasons across the whole run
# ---------------------------------------------------------------------------


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Consolidate per-test skip reasons into one counted digest for the run.

    pytest's -rs lists every skipped test individually; for a big suite this groups
    them by reason (exact match) so a missing env var / extension is obvious at a glance.
    """
    from collections import Counter

    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    counts = Counter()
    for rep in skipped:
        longrepr = getattr(rep, "longrepr", None)
        reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
        counts[reason.removeprefix("Skipped: ")] += 1

    # yellow to match pytest's own skip coloring (markup is honored per --color)
    terminalreporter.write_sep("-", f"skipped: {len(skipped)} by reason", yellow=True, bold=True)
    for reason, n in counts.most_common():
        terminalreporter.write_line(f"  {n:>4}  {reason}", yellow=True)

