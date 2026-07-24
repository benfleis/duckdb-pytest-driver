"""Generic pytest plugin / harness for duckdb test suites (the "driver" framework) — SLIMMED.

This is the pytest plugin, auto-registered via the ``pytest11`` entry point (see pyproject.toml). It
owns the hooks and the mechanisms the redesign KEEPS (option registration, binary resolution, `.test`
collection, working-dir/test-root resolution, per-run temp-dir lifecycle, the member/role/driver model
+ `run_paired`/`resources`, `--repl`, the store lifecycle, suite auto-marking + default-scan deselect,
the service provisioning entry, the report/terminal hooks). The collect-first *decision* — scan → plan
→ execute — lives in :mod:`ducktest.controller`; this module supplies the functions it delegates to.

Redesign deltas vs the shipped plugin (docs/SPEC.md, docs/ANALYSIS.md):
  * one typed :class:`~ducktest.context.SessionContext` on the pytest stash replaces the
    ``config._duckdb_*`` string-attribute sprawl;
  * the from-args predictor (`_suite_reachable`/`_markexpr_matches`) and the reactive
    `pytest_runtest_setup` credential backstop are GONE — the collect-first plan resolves `-k`
    directly, so up-front provisioning derives from the real selection;
  * the eager/on_demand service disposition is GONE — a service is provisioned up front from the plan
    (single entry :func:`provision_service`), and workers adopt its env from the store.

Import-name-agnostic on purpose (dual-mode): the same code works pip-installed as ``ducktest`` OR
vendored back into ``duckdb/test/py/``.
"""

import contextlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import warnings

import pytest

from . import store
from .collect import assign_batches, has_driver, is_driver  # noqa: F401 (re-exported: role model)
from .context import SessionContext, get_context, set_context
from .fixtures import duckdb_shell_for
from .mnemonic import run_id as _make_run_id
from .steps import step
from .suites import get_suites
from .sqllogic import (
    SqlLogicFile,
    SqlLogicItem,
    _cell_suffix,
    _invoke,
    _new_item,
    _parse_result,
    _raise_for_result,
    _test_batch_id,
    resolve_unittest_args,
)


# ---------------------------------------------------------------------------
# Option registration
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Register all driver options + ini settings (auto-called by pytest)."""
    register_options(parser)
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
        "Repo-relative dirs prepended to sys.path so test-local helper packages import. "
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
        help="Which build's test tools to use (default: auto). auto = the single built variant among "
        "(debug, release, reldebug, relassert), erroring if none or more than one. latest = the "
        "most-recently-built. Overridden by $BUILD_DIR and --unittest-bin / --duckdb-bin.",
    )
    parser.addoption(
        "--unittest-binary",
        "--unittest-bin",
        default=None,
        metavar="PATH",
        help="Explicit path to the unittest (Catch2-compatible) test binary. Overrides --build and $BUILD_DIR.",
    )
    parser.addoption(
        "--duckdb-bin",
        "--duckdb-binary",
        default=None,
        metavar="PATH",
        help="Explicit path to the duckdb shell. Overrides --build and $BUILD_DIR. Default: the `duckdb` "
        "next to the resolved unittest binary (build/<variant>/duckdb).",
    )
    parser.addoption(
        "--batch-size",
        default=10,
        type=int,
        metavar="N",
        help="Tests per unittest invocation (default: 10). Reduces subprocess overhead; use with -n.",
    )
    parser.addoption(
        "--collect-source",
        default="verify",
        choices=["verify", "authoritative"],
        help="How the FS gather and the binary's registered set (`unittest -l`) reconcile. verify "
        "(default): hard-error on divergence (a `.test_slow`/registered test the FS never collected — "
        "no silent false-green). authoritative: collect the union, the binary's set is truth.",
    )
    parser.addoption(
        "--existing-service",
        action="append",
        default=[],
        metavar="KEY[=URL|=JSON]",
        help="Attach to an ALREADY-RUNNING service instead of booting it (no boot, no store, no "
        "teardown). Repeatable; each value is a comma/semicolon list. Entry forms: KEY (all defaults), "
        "KEY=URL (endpoint override), KEY={json} (full override map). Also read from env "
        "DUCKTEST_EXISTING_SERVICE_<KEY> and DUCKTEST_EXISTING_SERVICES. See docs/SERVICES.md.",
    )
    parser.addoption(
        "--provision-service",
        nargs="?",
        const="*",
        default=None,
        metavar="KEY[,KEY]",
        help="OUT-OF-SESSION: start the named declared service(s) (all if no value) and LEAVE them "
        "running, then exit WITHOUT collecting or running tests. Idempotent. See docs/SERVICES.md.",
    )
    parser.addoption(
        "--teardown-service",
        nargs="?",
        const="*",
        default=None,
        metavar="KEY[,KEY]",
        help="OUT-OF-SESSION: stop the named declared service(s) (all if no value), then exit.",
    )
    parser.addoption(
        "--unittest-args",
        action="append",
        default=[],
        metavar="ARGS",
        help="Extra argument(s) appended verbatim to EVERY unittest binary invocation. Repeatable; "
        "each value is shlex-split. Generic passthrough for binary flags the driver does not model.",
    )
    parser.addoption(
        "--temp-dir-base",
        default=None,
        metavar="ROOT",
        help="This run's `<root>` (may be local OR remote, e.g. s3://…). `_invoke` composes the per-"
        "invocation --temp-dir-base = <root>/<session-id>/<batch-id> from it; the binary appends the "
        "<test-id> leaf and owns LOCAL create/sweep. The driver sweeps only a REMOTE <root>, once at "
        "session end (SPEC §11.5). Default: the binary's own `duckdb_unittest_tempdir`.",
    )
    parser.addoption(
        "--data-dir",
        default=None,
        metavar="DIR",
        help="Read-only root for test input data, passed to the binary as --data-dir (DATA_DIR). A plain "
        "input path — never composed with session-id/batch-id, never swept. Default: the binary's "
        "working_dir/data.",
    )
    parser.addoption(
        "--temp-dir-destroy",
        default="on-success",
        choices=["never", "on-success", "always"],
        metavar="{never,on-success,always}",
        help="Destroy disposition for the LOCAL per-batch run-root: never | on-success (default) | always. "
        "Passed THROUGH to the binary, which owns the LOCAL sweep (SPEC §11.5). The REMOTE sweep is "
        "keep-on-failure by construction (the driver keeps a failed test's batch), not gated by this.",
    )
    parser.addoption(
        "--temp-sweep-age-days",
        default=7,
        type=int,
        metavar="N",
        help="Age-sweep backstop (SPEC §11.5): purge REMOTE `<root>/<old-session-id>` prefixes older than N "
        "days (default 7). Best-effort, controller-only, and only when the registered sweeper supports "
        "listing run prefixes.",
    )
    # --- @requires-driven provisioning / interactive shell ------------------
    parser.addoption(
        "--repl",
        action="store_true",
        default=False,
        help="For the SINGLE selected test, read its @requires, provision its fixtures, and drop into "
        "an interactive shell attached to them. Tears down on exit (unless --provision-keep).",
    )
    parser.addoption(
        "--provision-keep",
        action="store_true",
        default=False,
        help="With --repl: do NOT tear down provisioned fixtures on exit; print the teardown command instead.",
    )
    parser.addoption(
        "--provision-dry-run",
        action="store_true",
        default=False,
        help="With --repl: print the resolved @requires specs, the provision plan and the would-be "
        "duckdb init SQL, then stop. Performs NO DDL, does NOT launch the shell, does NOT tear down.",
    )
    parser.addoption(
        "--steps",
        action="store_true",
        default=False,
        help="Surface driver step() messages live (provisioning, clones, teardown, with timings). "
        "Forces single-process (-n0). --repl / --provision-keep auto-enable this; pass --no-steps to opt out.",
    )
    parser.addoption(
        "--no-steps",
        action="store_true",
        default=False,
        help="Suppress the step() narration that --repl / --provision-keep auto-enable.",
    )
    parser.addoption(
        "--emit-plan",
        metavar="PATH",
        default=None,
        help="Emit the collect-first scan/plan as JSON to PATH (or stdout if PATH is '-'): the "
        "selected node-ids, reachable suites, needed credentials/services, and the FS-vs-binary "
        "collection divergence. Inspection/debugging; also the plan-as-artifact hand-off (SPEC §10.5).",
    )


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

BUILD_VARIANTS = ("debug", "release", "reldebug", "relassert")


def _variant_binary(working_dir, variant):
    return os.path.join(working_dir, "build", variant, "test", "unittest")


def _built_variants(working_dir):
    out = []
    for v in BUILD_VARIANTS:
        p = _variant_binary(working_dir, v)
        if os.path.isfile(p):
            out.append((v, p, os.path.getmtime(p)))
    return out


def _variants_listing():
    return ", ".join("build/%s/test/unittest" % v for v in BUILD_VARIANTS)


def find_binary(config, working_dir):
    """Return the unittest binary path (--unittest-binary > $BUILD_DIR > --build)."""
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
                f"--build latest: no unittest binary in any of {_variants_listing()} (under {working_dir})."
            )
        return max(found, key=lambda t: t[2])[1]

    return _variant_binary(working_dir, build)


def find_duckdb(config, working_dir):
    """Return the duckdb shell path (--duckdb-bin > $BUILD_DIR > the `duckdb` next to the unittest binary)."""
    explicit = config.getoption("--duckdb-bin", default=None)
    if explicit:
        path = os.path.abspath(explicit)
    else:
        build_dir = os.environ.get("BUILD_DIR")
        path = os.path.join(build_dir, "duckdb") if build_dir else duckdb_shell_for(find_binary(config, working_dir))
    if not os.path.isfile(path):
        raise pytest.UsageError(
            f"duckdb shell not found at {path}. Build build/<variant>/duckdb (selected by --build / "
            "$BUILD_DIR), or pass --duckdb-bin PATH."
        )
    return path


def binary_names_if_any(config):
    """The binary's registered set (`unittest -l`) for the collect-first reconcile, or empty when there
    is no resolvable/present binary (a pure-Python or no-binary run needn't fail for lack of one)."""
    from .collect import list_binary_tests

    working_dir = _working_dir(config)
    try:
        binary = find_binary(config, working_dir)
    except pytest.UsageError:
        return frozenset()
    if not os.path.isfile(binary):
        return frozenset()
    try:
        return list_binary_tests(binary, working_dir=working_dir)
    except RuntimeError:
        return frozenset()  # too-old binary: not a false-green source, so don't abort


def reconcile_or_die(config, plan):
    """verify mode: refuse a false-green run where the binary knows tests the FS didn't collect."""
    if not plan.binary_names:
        return  # no `.test` lane / no binary — nothing to reconcile
    from .collect import reconcile

    mode = get_context(config).options.get("collect_source", "verify")
    reconcile(plan.fs_names, plan.binary_names, mode=mode).check()


def pytest_report_header(config):
    """Session-header lines: the mandatory suite banner (always) + a verbose (`-v`) tool trace."""
    lines = []
    banner = _suite_banner(config)
    if banner:
        lines.append(banner)
    if int(config.getoption("verbose", default=0) or 0) >= 1:
        working_dir = _maybe_working_dir(config) or os.getcwd()
        for label, resolve in (("unittest", find_binary), ("duckdb", find_duckdb)):
            try:
                lines.append(f"duckdb-pytest-driver {label}: {resolve(config, working_dir)}")
            except Exception:
                pass  # not resolvable yet (e.g. no binary for a pure-collection run) — skip
    return lines or None


# ---------------------------------------------------------------------------
# Working dir / test root resolution + collection
# ---------------------------------------------------------------------------


def _resolve_working_dir(config):
    cli = config.getoption("--duckdb-working-dir", default=None)
    if cli:
        return os.path.abspath(cli)
    ini = config.getini("duckdb_working_dir")
    if ini:
        return os.path.abspath(ini)
    return str(config.rootpath)


def _working_dir(config):
    """The resolved working dir, cached on config as `sqllogic_working_dir` (read by run_paired/--repl)."""
    wd = getattr(config, "sqllogic_working_dir", None)
    if wd is None:
        wd = _resolve_working_dir(config)
        config.sqllogic_working_dir = wd
    return wd


def _maybe_working_dir(config):
    return getattr(config, "sqllogic_working_dir", None)


def _test_root(config):
    cli = config.getoption("--duckdb-test-root", default=None)
    if cli:
        return os.path.abspath(cli)
    ini = config.getini("duckdb_test_root")
    if ini:
        return os.path.abspath(ini)
    return os.path.join(_working_dir(config), "test")


def _ensure_pythonpath(config):
    """Prepend the configured test-local import roots (`duckdb_pythonpath`) to sys.path (idempotent)."""
    working_dir = _working_dir(config)
    for rel in config.getini("duckdb_pythonpath"):
        p = os.path.join(working_dir, rel)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


_CONTROLLER_PLUGIN_NAME = "ducktest_controller"


@pytest.hookimpl(hookwrapper=True)
def pytest_load_initial_conftests(early_config, parser, args):
    # Prepend the test-local import roots BEFORE any initial conftest is imported.
    _ensure_pythonpath(early_config)
    # Register the collect-first Controller ONLY on the controller (not xdist workers): a worker
    # collects normally and consumes the plan/store the controller published. Gate on workerinput.
    if getattr(early_config, "workerinput", None) is None:
        from .controller import Controller

        if not early_config.pluginmanager.hasplugin(_CONTROLLER_PLUGIN_NAME):
            early_config.pluginmanager.register(Controller(), _CONTROLLER_PLUGIN_NAME)
    yield


def pytest_exception_interact(node, call, report):
    """Render provisioning/credential INFRA failures as their one-line reason, not a traceback.

    ProvisionFailed / ProvisionTimeout / ResourceMissing are store signals, not code bugs (a service
    never became ready, a credential fetch failed as the poison-pill owner, a resource was never
    provisioned) — the store/fixture frames are pure noise. Collapse them to the message for EVERY
    consumer, at any phase (setup/call/teardown). The exception type is untouched (still catchable
    upstream); only its rendering changes — so a consumer needs no per-fixture `try/except` to get a
    clean, loud failure.
    """
    excinfo = getattr(call, "excinfo", None)
    if excinfo is not None and excinfo.errisinstance(
        (store.ProvisionFailed, store.ProvisionTimeout, store.ResourceMissing)
    ):
        report.longrepr = str(excinfo.value)


def pytest_ignore_collect(collection_path, config):
    """Skip the configured top-level dirs under working_dir (default: duckdb/, build/)."""
    working_dir = _working_dir(config)
    target = str(collection_path)
    for name in config.getini("duckdb_ignore_dirs"):
        if target == os.path.join(working_dir, name):
            return True
    return None


def pytest_collect_file(parent, file_path):
    """Collect `.test` bodies and same-stem `.py` drivers under the test root (role/member model)."""
    config = parent.config
    working_dir = _working_dir(config)
    test_root = _test_root(config)
    if not str(file_path).startswith(test_root + os.sep):
        return None
    if file_path.suffix == ".py":
        if is_driver(str(file_path)):
            return pytest.Module.from_parent(parent, path=file_path)
        return None
    if file_path.suffix != ".test":
        return None
    if has_driver(str(file_path)):
        return None  # runs only via its same-stem driver
    binary = find_binary(config, working_dir)
    return SqlLogicFile.from_parent(parent, path=file_path, binary=binary, working_dir=working_dir)


# ---------------------------------------------------------------------------
# Per-run temp dir + run-id
# ---------------------------------------------------------------------------


def _run_id(config):
    """This run's id, cached on config; shared across xdist workers (broadcast via workerinput)."""
    cached = getattr(config, "_sqllogic_run_id", None)
    if cached is not None:
        return cached
    wi = getattr(config, "workerinput", None)
    run_id = wi["sqllogic_run_id"] if wi and "sqllogic_run_id" in wi else _make_run_id()
    config._sqllogic_run_id = run_id
    return run_id


# ---------------------------------------------------------------------------
# TEMP/DATA storage inputs (SPEC §11.2/§11.3). The driver originates ONLY the run's
# `root` + `session-id` (the run mnemonic, via `_run_id` — broadcast to every xdist
# worker so the whole run shares ONE identity) plus an optional read-only DATA dir.
# It does NOT compose any full path here: `_invoke` composes the ONE per-invocation
# --temp-dir-base = <root>/<session-id>/<batch-id> (SPEC §11.4). The BINARY appends
# the <test-id> leaf, derives LOCAL_*, and owns local create/sweep — the driver never
# composes LOCAL_*, never sets a TEMP_DIR env var. The mnemonic session-id is what
# makes the run's dirs one shared, sweepable set across workers (not pid-tagged).
# ---------------------------------------------------------------------------

# A root is REMOTE when it carries a URI scheme other than file:// (s3://, abfss://,
# gs://, az://, …). A bare path or a file:// URI is LOCAL.
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")


def _is_remote_root(value):
    if not value:
        return False
    m = _URI_SCHEME.match(value)
    return bool(m) and m.group(1).lower() != "file"


def _root(config):
    """This run's `<root>` — the outer base under which `_invoke` composes `<root>/<session-id>/
    <batch-id>` (SPEC §11.2). The explicit `--temp-dir-base` (may be local OR remote, e.g. s3://…),
    else the binary's own default temp-dir name (`duckdb_unittest_tempdir`, resolved by the binary
    relative to its working dir)."""
    base = config.getoption("--temp-dir-base", default=None)
    return base if base else "duckdb_unittest_tempdir"


def _temp_roots(config):
    """Originate the run's storage inputs the driver hands the binary (SPEC §11.2/§11.3), cached on config.

    The driver originates only `root` + `session_id` (the run mnemonic, via `_run_id` — broadcast to
    every xdist worker so the whole run shares ONE identity) plus an optional read-only DATA dir. NO
    full-path composition here and NO LOCAL_*: `_invoke` composes the per-invocation --temp-dir-base =
    `<root>/<session-id>/<batch-id>`, and the BINARY appends the `<test-id>` leaf + derives LOCAL_*.

    Returns the driver's bookkeeping dict:
      * `root`       → the outer base; also the REMOTE sweeper's `<root>` (it sweeps `<root>/<session-id>/`);
      * `session_id` → the run mnemonic, the `<session-id>` level (date-sortable; the age-sweep sorts on it);
      * `data_dir`   → --data-dir when set, else None (the binary defaults to working_dir/data) — a plain
                       read-only path, never composed with the session-id/batch-id, never swept;
      * `destroy`    → the --temp-dir-destroy disposition, passed through to gate the binary's LOCAL sweep.
    """
    cached = getattr(config, "_sqllogic_temp_roots", None)
    if cached is not None:
        return cached
    roots = {
        "root": _root(config),
        "session_id": _run_id(config),
        "data_dir": config.getoption("--data-dir", default=None),
        "destroy": config.getoption("--temp-dir-destroy", default="on-success"),
    }
    config._sqllogic_temp_roots = roots
    return roots


# ---------------------------------------------------------------------------
# Controller -> worker broadcast (credentials-class primitive; config-attribute based so a plain
# stand-in Config works — see test_broadcast)
# ---------------------------------------------------------------------------
_BROADCAST_FACTORIES = "_duckdb_broadcast_factories"
_BROADCAST_CACHE = "_duckdb_broadcast_cache"


def register_broadcast(config, key, factory):
    """Register `factory(config) -> picklable` to compute `key` ONCE on the controller and broadcast it
    to all xdist workers. No-op on a worker (the value arrives via workerinput)."""
    if getattr(config, "workerinput", None) is not None:
        return
    reg = getattr(config, _BROADCAST_FACTORIES, None)
    if reg is None:
        reg = {}
        setattr(config, _BROADCAST_FACTORIES, reg)
    reg[key] = factory


def get_broadcast(config, key, default=None):
    """Value for `key`: on a worker, the controller's broadcast; on the controller, computed once (cached)."""
    wi = getattr(config, "workerinput", None)
    if wi is not None and key in wi:
        return wi[key]
    cache = getattr(config, _BROADCAST_CACHE, None)
    if cache is None:
        cache = {}
        setattr(config, _BROADCAST_CACHE, cache)
    if key not in cache:
        factory = (getattr(config, _BROADCAST_FACTORIES, None) or {}).get(key)
        cache[key] = factory(config) if factory else default
    return cache[key]


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    # xdist controller hook: hand each worker the run-id + any registered broadcast values.
    node.workerinput["sqllogic_run_id"] = _run_id(node.config)
    for key in getattr(node.config, _BROADCAST_FACTORIES, None) or {}:
        node.workerinput[key] = get_broadcast(node.config, key)


# ---------------------------------------------------------------------------
# The store lifecycle (on the SessionContext, not config._duckdb_* strings)
# ---------------------------------------------------------------------------


class _StoreHandle:
    """The controller's live store: the SyncManager (owns the server process), a cached proxy, and the
    address/authkey published to the env pre-fork so workers connect. Held on `ctx.store`."""

    __slots__ = ("mgr", "proxy", "address", "authkey")

    def __init__(self, mgr, proxy, address, authkey):
        self.mgr = mgr
        self.proxy = proxy
        self.address = address
        self.authkey = authkey


def _any_suite_has_resources(config):
    """True iff some registered suite declares a credential or a service (the store's raison d'être)."""
    return any(t.credentials or t.services for t in get_suites(config))


def get_store(config):
    """The shared-state store PROXY for this run, or None if no store was started.

    Controller: the proxy stashed on the context when the store started (None if vanilla). Worker:
    lazily connects once via the address the controller published to the env, caching on the context.
    Use with the store access verbs (`store.copy` / `store.copy_or_provision`).
    """
    ctx = get_context(config)
    if ctx.store is not None:
        return ctx.store.proxy
    if getattr(config, "workerinput", None) is None:
        return None  # controller: a handle is stashed at start-time; its absence => no store started
    loc = store.from_env()
    if loc is None:
        return None  # no store address published this run (vanilla)
    mgr = store.connect(*loc)
    proxy = mgr.store()
    ctx.store = _StoreHandle(mgr, proxy, *loc)
    return proxy


def _ensure_store_started(config):
    """Controller, at trylast configure (pre-fork): start the store + publish its address to the env,
    but ONLY when a suite declares a credential or service. Vanilla stays vanilla (no store, no env)."""
    ctx = get_context(config)
    if ctx.store is not None:
        return ctx.store
    if not _any_suite_has_resources(config):
        return None
    mgr, address, authkey = store.start_server()
    ctx.store = _StoreHandle(mgr, mgr.store(), address, authkey)
    os.environ.update(store.to_env(address, authkey))  # pre-fork: workers inherit + connect lazily
    return ctx.store


def _teardown_store(config):
    """Controller, at sessionfinish: stop started services, then shut the store manager down (idempotent)."""
    ctx = get_context(config)
    if ctx.store is None:
        return
    _stop_services(config)
    try:
        ctx.store.mgr.shutdown()
    except Exception:
        pass  # already down / never fully started — teardown must not raise at session end
    ctx.store = None


# ---------------------------------------------------------------------------
# Existing (external) services: attach instead of boot
# ---------------------------------------------------------------------------
_TRUTHY = {"", "1", "true", "yes", "on"}


def _norm_service_key(key):
    """Canonical service-key form (``oss-uc-server`` == ``OSS_UC_SERVER`` == ``oss_uc_server``)."""
    return key.replace("-", "_").lower()


def _existing_entry_value(val):
    import json

    v = (val or "").strip()
    if v in _TRUTHY:
        return {}
    if v.startswith("{"):
        return json.loads(v)
    return {"endpoint": v}


def _parse_existing_services(cli_values, environ):
    """Resolve existing-service declarations to ``{norm_key: overrides}`` (pure, unit-testable).

    Precedence low->high: DUCKTEST_EXISTING_SERVICES (list env) < per-service env
    DUCKTEST_EXISTING_SERVICE_<KEY> < --existing-service (CLI). Entry grammar: KEY | KEY=URL | KEY={json}.
    """

    def _split(s):
        out, buf, depth, in_str, escape = [], [], 0, False, False
        for ch in s or "":
            if in_str:
                buf.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                buf.append(ch)
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
            if ch in ",;" and depth == 0:
                part = "".join(buf).strip()
                if part:
                    out.append(part)
                buf = []
            else:
                buf.append(ch)
        part = "".join(buf).strip()
        if part:
            out.append(part)
        return out

    def _add(result, entry):
        key, sep, val = entry.partition("=")
        key = key.strip()
        if key:
            result[_norm_service_key(key)] = _existing_entry_value(val if sep else "")

    result = {}
    for entry in _split(environ.get("DUCKTEST_EXISTING_SERVICES", "")):  # lowest: list env
        _add(result, entry)
    prefix = "DUCKTEST_EXISTING_SERVICE_"  # per-service env (DUCKTEST_EXISTING_SERVICES lacks the '_')
    for name, val in environ.items():
        if name.startswith(prefix) and name[len(prefix) :]:
            result[_norm_service_key(name[len(prefix) :])] = _existing_entry_value(val)
    for value in cli_values or []:  # highest: CLI (each value may itself be a list)
        for entry in _split(value):
            _add(result, entry)
    return result


def _existing_services(config):
    """Cached ``{norm_key: overrides}`` for this run (from ctx.options; parsed once at configure)."""
    return get_context(config).existing_services


def _attach_service(config, svc, overrides):
    """Build an EXISTING service's block from ``overrides`` + probe it — no boot, no store, no teardown.

    If the service declares an ``alive`` probe and it reports dead, FAIL LOUD — a selected test whose
    declared service isn't reachable fails clearly rather than dying opaquely deep in a query.
    """
    block = svc.attach(dict(overrides), config) if svc.attach is not None else dict(overrides)
    if svc.alive is not None and not svc.alive(block):
        where = (block or {}).get("endpoint") or "<default endpoint>"
        pytest.fail(
            f"--existing-service {svc.key}: declared as running at {where}, but nothing is responding "
            f"there. Start it, or drop --existing-service {svc.key} to let this run boot it.",
            pytrace=False,
        )
    block = _service_block(svc, {**block, "attached": True, "started": False})
    _run_populate(config, svc, block)  # idempotent — an externally-owned instance may already be seeded
    return block


# ---------------------------------------------------------------------------
# Service provisioning (single entry, no disposition)
# ---------------------------------------------------------------------------


def _service_block(svc, extra):
    """Normalize ``svc.start``'s return into a stored dict block carrying ``{key, started}``."""
    block = {"key": svc.key, "started": True}
    if isinstance(extra, dict):
        block.update(extra)
    return block


def _run_populate(config, svc, block):
    """Run ``svc.populate`` (structure+data) if declared. Idempotent by contract."""
    if svc.populate is not None:
        with step(f"populating service {svc.key}"):
            svc.populate(block, config)


def _boot_and_populate(config, svc):
    """The store single-flight factory: ``start`` then ``populate``, both exactly once (first-worker-wins)."""
    block = _service_block(svc, svc.start(config))
    _run_populate(config, svc, block)
    return block


def _resolve_service(config, key):
    """The registered `Service` descriptor for `key` (any suite), or None if undeclared."""
    for suite in get_suites(config):
        for svc in suite.services:
            if svc.key == key:
                return svc
    return None


def provision_service(config, svc, *, _visiting=()):
    """Provision a class-2 service — the ONE routing point (docs/SPEC.md §3.6). Attach-or-boot:

    - **existing** (``--existing-service <key>`` / env): attach (build block + probe ``alive`` +
      populate); no boot, no store, no teardown.
    - **no ``start`` at all** (``svc.start is None``): ``invocation-external`` (RESOURCE-PLANNING.md
      phase 5) — the service has NO managed lifecycle, it just permanently exists, so this routes
      straight to ``attach()`` with all-defaults overrides UNCONDITIONALLY, same as a declared
      ``--existing-service`` with no override. No store, no teardown, no boot attempt (there's
      nothing to boot).
    - **managed** (default, ``start`` given): single-flight boot via the store — the first caller
      runs ``start`` + ``populate`` under the per-key lock and publishes the block; concurrent
      callers read it.

    ``depends_on`` (RESOURCE-PLANNING.md phase 6, subsumes ``docs/PLAN.md``'s Iceberg
    ``rest``→``minio`` case): before any of the above, every key in ``svc.depends_on`` is
    provisioned FIRST, recursively — start order falls out of the recursion by construction, no
    separate scheduling pass needed. A cycle (A depends on B depends on A) fails loud
    (``pytest.UsageError``) instead of recursing forever; ``_visiting`` is the internal
    cycle-detection breadcrumb — never pass it yourself.

    Adopts ``to_env`` into THIS process's ``os.environ`` (how a test — even a bare ``.test`` — gets a
    service's connection env). Returns the block; a test cannot tell which stance ran (identical shape).
    Idempotent: called both up front (from the collect-first plan) and on a fixture pull.
    """
    if svc.depends_on:
        if svc.key in _visiting:
            chain = " -> ".join((*_visiting, svc.key))
            raise pytest.UsageError(f"service depends_on cycle detected: {chain}")
        visiting = (*_visiting, svc.key)
        for dep_key in svc.depends_on:
            dep = _resolve_service(config, dep_key)
            if dep is None:
                raise pytest.UsageError(
                    f"service {svc.key!r} declares depends_on={dep_key!r}, but no service with that key is registered"
                )
            provision_service(config, dep, _visiting=visiting)

    existing = _existing_services(config)
    if _norm_service_key(svc.key) in existing:
        block = _attach_service(config, svc, existing[_norm_service_key(svc.key)])
    elif svc.start is None:
        block = _attach_service(config, svc, {})
    else:
        handle = get_store(config)
        if handle is None:
            block = _boot_and_populate(config, svc)  # no store (nothing declared): local boot
        else:
            block = store.copy_or_provision(handle, svc.key, lambda: _boot_and_populate(config, svc))
    if svc.to_env is not None:
        os.environ.update({k: str(v) for k, v in svc.to_env(block).items()})
    return block


def _all_services(config):
    """Every distinct declared `Service` across all suites, dedup by key (first registration wins)
    — the full node set `depends_on` ordering (phase 6) resolves over."""
    seen, out = set(), []
    for suite in get_suites(config):
        for svc in suite.services:
            if svc.key in seen:
                continue
            seen.add(svc.key)
            out.append(svc)
    return out


def _topological_service_order(services):
    """`services`, ordered so every service comes after everything in its `depends_on` — Kahn's
    algorithm, deterministic (ties broken by input order), cycle-checked: a service whose
    `depends_on` can never be satisfied (an undeclared key, or a cycle among `services`) raises
    loud rather than silently dropping it.
    """
    by_key = {s.key: s for s in services}
    indegree = {s.key: 0 for s in services}
    children: dict = {s.key: [] for s in services}
    for s in services:
        for dep in s.depends_on:
            if dep not in by_key:
                raise pytest.UsageError(
                    f"service {s.key!r} declares depends_on={dep!r}, but no service with that key is registered"
                )
            indegree[s.key] += 1
            children[dep].append(s.key)
    ready = [s.key for s in services if indegree[s.key] == 0]
    order = []
    while ready:
        key = ready.pop(0)
        order.append(key)
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(order) != len(services):
        cyclic = sorted(set(by_key) - set(order))
        raise pytest.UsageError(f"service depends_on cycle detected among: {cyclic}")
    return [by_key[k] for k in order]


def _stop_services(config):
    """Controller, at sessionfinish (pre store-shutdown): stop each STARTED service, in REVERSE
    topological order (`depends_on`, phase 6) — a dependency (e.g. `minio`) is stopped only after
    everything that depends on it (e.g. `rest`) already has been, the reverse of
    `provision_service`'s recursive start order.

    A block exists in the store under a service's key iff some worker provisioned it, so store-presence
    == started; dedup by key (a shared service is ONE physical resource). The topological sort runs
    over only the STARTED subset (never the full declared set) — `provision_service` always
    provisions a service's dependencies before itself, so a started service's dependencies are
    necessarily started too, meaning an unreached/never-provisioned service (however its
    `depends_on` is shaped) can never surface a spurious cycle/undeclared-key error at teardown.
    """
    handle = get_store(config)
    if handle is None:
        return
    started = []
    for svc in _all_services(config):
        try:
            store.copy(handle, svc.key)  # present => was provisioned this run
        except store.ResourceMissing:
            continue
        started.append(svc)
    for svc in reversed(_topological_service_order(started)):
        if svc.stop is None:
            continue
        try:
            svc.stop(config)
        except Exception:
            pass  # teardown must not raise at session end


# ---------------------------------------------------------------------------
# Up-front provisioning from the collect-first plan (replaces the predictor + backstop)
# ---------------------------------------------------------------------------


def _fetch_or_read_credential(config, cred):
    """Fetch (or read from the store) a credential, validate it, and adopt it into the env.

    Single-flighted through the store so a `-k`-selected run prompts at most once (the poison pill on
    the store makes an invalid fetch fail every waiter). ``available()`` short-circuits when the creds
    are already usable in the env (no fetch/op). An invalid credential aborts the session (UsageError).
    """
    if cred.available is not None and cred.available():
        return  # already usable in the env (non-interactive) — no fetch needed
    handle = get_store(config)
    if handle is not None:
        block = store.copy_or_provision(handle, cred.key, lambda: cred.fetch(config))
    else:
        block = cred.fetch(config)
    if cred.validate is not None and not cred.validate(block):
        raise pytest.UsageError(cred.error() if cred.error else f"{cred.key}: required credential unavailable")
    if cred.adopt == "env":
        os.environ.update({k: str(v) for k, v in block.items()})


def _read_provisioned_credential(config, cred):
    """Best-effort read of a credential's ALREADY-fetched value, with no new fetch (no repeat `op`/CLI
    prompt) — for `--repl`'s init-SQL gather, which runs after up-front provisioning already fetched it.
    None if there's no store (no suite anywhere declares a credential/service) or it was never fetched
    (e.g. `available()` short-circuited it — the value was never broadcast, only the env)."""
    handle = get_store(config)
    if handle is None:
        return None
    try:
        return store.copy(handle, cred.key)
    except store.ResourceMissing:
        return None


def emit_plan(config, plan):
    """Write `plan` as JSON to `--emit-plan`'s PATH (stdout if '-'); no-op if the flag is unset. Runs
    on the controller only (where the Plan is built), so exactly one artifact is emitted per run."""
    dest = config.getoption("--emit-plan", default=None)
    if not dest:
        return
    import json

    payload = json.dumps(plan.as_dict(), indent=2, sort_keys=True)
    if dest == "-":
        print("\n" + payload)
    else:
        with open(dest, "w") as f:
            f.write(payload + "\n")


def _for_each_reachable_resource(config, reachable_suite_names, *, on_credential, on_service):
    """The shared gather: walk every reachable suite's credentials/services, each exactly ONCE
    (dedup by key — a shared service/credential is ONE resource), calling `on_credential(cred)` /
    `on_service(svc)`. `provision_reachable` and `_repl_resource_init_sql` were two structurally
    near-identical copies of this loop (RESOURCE-PLANNING.md §1, problem 3); this is the one they
    both drive, differing only in what a callback does with the resource.
    """
    suites = {t.name: t for t in get_suites(config)}
    seen_creds, seen_svcs = set(), set()
    for name in sorted(reachable_suite_names):
        suite = suites.get(name)
        if suite is None:
            continue
        for cred in suite.credentials:
            if cred.key in seen_creds:
                continue
            seen_creds.add(cred.key)
            on_credential(cred)
        for svc in suite.services:
            if svc.key in seen_svcs:
                continue
            seen_svcs.add(svc.key)
            on_service(svc)


def provision_reachable(config, reachable_suite_names):
    """Provision everything the reachable suites need: fetch their credentials, provision their
    services, adopt env. Dedup by key (a shared service/credential is ONE resource). The single
    up-front execute path; workers run it too (from their own selection), coordinated by the store.
    """

    def _on_service(svc):
        try:
            provision_service(config, svc)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # Up-front service provisioning is best-effort pre-warm: a failure (e.g. a dead
            # --existing-service, which raises pytest.fail's BaseException-derived Failed) is
            # re-surfaced authoritatively when a fixture pulls it or a test uses its env — as a
            # per-test setup error — not a whole-collection abort here.
            pass

    _for_each_reachable_resource(
        config,
        reachable_suite_names,
        on_credential=lambda cred: _fetch_or_read_credential(config, cred),
        on_service=_on_service,
    )


def _reachable_suites(config, items):
    """Suites reachable for THIS process's selection: a default suite on a bare run, or any suite an
    actually-selected item belongs to (by auto-marker or path). This is collect-first, so `-k` is
    resolved by real membership — no from-args predictor."""
    bare = _no_explicit_selection(config)
    names = set()
    for suite in get_suites(config):
        if bare and suite.default:
            names.add(suite.name)
            continue
        for item in items:
            if _item_in_suite(config, item, suite):
                names.add(suite.name)
                break
    return names


def _repl_resource_init_sql(config, session, *, redact=False) -> str:
    """`--repl` init SQL contributed by reachable suites' active services/credentials — the
    service/credential analog of a `Provisioner`'s `make_init_sql`, for a suite that has no provisioner
    at all (bare-`.test` suites like azurite/az/minio/s3). Without this, dropping into `--repl` on such
    a suite gives you the connection env but no secret/`USE` already typed (unlike a `@requires`-driven
    suite, whose provisioner's `make_init_sql` covers it).

    Everything here was already provisioned up front by `provision_reachable` (same reachability gate),
    so this reads back what's there — it fetches/boots nothing new, and a credential with no
    `to_init_sql` (or never actually fetched, e.g. `available()` short-circuited it) just contributes
    nothing, same as a service with no `to_init_sql`.

    UNLIKE `provision_reachable`'s best-effort service loop, a service that fails to (re-)provision
    here FAILS LOUD (`pytest.UsageError`, not swallowed): `--repl` is an interactive human command with
    no later fixture-pull to resurface the error, so a swallowed failure would just be a silent,
    unexplained bare shell — worse than no feature at all.
    """
    parts = []

    def _on_credential(cred):
        if cred.to_init_sql is None:
            return
        value = _read_provisioned_credential(config, cred)
        if value is not None:
            parts.append(cred.to_init_sql(value, redact=redact))

    def _on_service(svc):
        if svc.to_init_sql is None:
            return
        try:
            block = provision_service(config, svc)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise pytest.UsageError(
                f"--repl: service {svc.key!r} failed to provision (needed for its --repl init SQL): {exc}"
            ) from exc
        parts.append(svc.to_init_sql(block, redact=redact))

    _for_each_reachable_resource(
        config, _reachable_suites(config, session.items), on_credential=_on_credential, on_service=_on_service
    )
    return "".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# auto_init_sql: the non-`--repl` generalization of `to_init_sql` (RESOURCE-PLANNING.md §5 phase 9,
# folded in alongside the suite-matrix work). A suite opts in (`register_suite(auto_init_sql=True)`)
# so a bare `.test` item gets its credentials'/services' `to_init_sql` output run via upstream's
# `--init-sqllogic` (one `statement ok` block, executed before the test body, in the SAME runner
# instance/process) instead of requiring the body to hand-write its own `CREATE SECRET`.
# ---------------------------------------------------------------------------


def _suite_init_sql(config, suite) -> str:
    """Aggregate `suite`'s OWN credentials'/services' `to_init_sql` output (never redacted — this
    feeds a real subprocess, not a preview). Same "read back what provision_reachable already
    provisioned up front" spirit as `_repl_resource_init_sql`, just scoped to one suite instead of
    every reachable one — `auto_init_sql` opts in per suite, not per invocation.

    Best-effort per service (unlike `_repl_resource_init_sql`'s fail-loud `--repl` path): a service
    that fails to reprovision here just contributes nothing, since there's no interactive user to
    surface the error to — the test itself will fail clearly once it tries to use the missing secret.
    """
    parts = []
    for cred in suite.credentials:
        if cred.to_init_sql is None:
            continue
        value = _read_provisioned_credential(config, cred)
        if value is not None:
            parts.append(cred.to_init_sql(value, redact=False))
    for svc in suite.services:
        if svc.to_init_sql is None:
            continue
        try:
            block = provision_service(config, svc)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            continue
        parts.append(svc.to_init_sql(block, redact=False))
    return "".join(p for p in parts if p)


def _write_init_sqllogic_snippet(sql_text: str) -> str:
    """One `statement ok` block wrapping `sql_text` verbatim, as a `--init-sqllogic`-ready `.test`
    file. DuckDB's `Query()` executes a `;`-separated multi-statement batch in one call, so a
    `to_init_sql` producing more than one statement needs no per-statement splitting here.

    Left on disk for the rest of the invocation (a few bytes, at most one per opted-in suite per
    worker process) — not worth the LOCAL_*/DATA sweep machinery real test data already has.
    """
    fd, path = tempfile.mkstemp(suffix=".test", prefix="ducktest-init-sqllogic-")
    with os.fdopen(fd, "w") as f:
        f.write(f"statement ok\n{sql_text}\n")
    return path


_init_sqllogic_paths: dict = {}  # (id(config), suite.name) -> temp .test path, or "" (nothing to inject)


def _init_sqllogic_arg_for_item(config, item) -> list:
    """`["--init-sqllogic", path]` for a bare `.test` item whose suite opts in via
    `auto_init_sql=True`; `[]` for a non-opted-in suite, or one with nothing to inject (no
    credential/service `to_init_sql`, or none currently provisioned).

    Memoized per `(config, suite)` for this worker process — `_suite_init_sql` reads back already-
    provisioned resources, so recomputing it per test would just rebuild the identical string.
    """
    suites = [s for s in get_suites(config) if s.auto_init_sql]
    if not suites:
        return []
    suite = next((s for s in suites if _item_in_suite(config, item, s)), None)
    if suite is None:
        return []
    key = (id(config), suite.name)
    path = _init_sqllogic_paths.get(key)
    if path is None:
        sql = _suite_init_sql(config, suite)
        path = _write_init_sqllogic_snippet(sql) if sql else ""
        _init_sqllogic_paths[key] = path
    return ["--init-sqllogic", path] if path else []


# ---------------------------------------------------------------------------
# Out-of-session service lifecycle commands (--provision-service / --teardown-service)
# ---------------------------------------------------------------------------


def _service_targets(config, spec):
    """The (suite, service) pairs a provision/teardown command targets (all if ``*``/empty; else the
    ones whose normalized key is in the comma/semicolon list). Deduplicated by service ``key``."""
    import re

    seen, services = set(), []
    for t in get_suites(config):
        for s in t.services:
            if s.key in seen:
                continue
            seen.add(s.key)
            services.append((t, s))
    if spec in (None, "*", ""):
        return services
    keys = {_norm_service_key(k) for k in re.split(r"[,;]", spec) if k.strip()}
    return [(t, s) for (t, s) in services if _norm_service_key(s.key) in keys]


def run_service_command(config):
    """Out-of-session lifecycle: provision or tear down declared services, then ``pytest.exit``.

    Runs on the controller (trylast configure), BEFORE collection — so it needs no unittest binary.
    **provision** starts each target directly (NOT via the store), so sessionfinish leaves it running;
    idempotent via the ``alive`` probe. **teardown** stops each target.
    """
    teardown = config.getoption("--teardown-service", default=None)
    prov = config.getoption("--provision-service", default=None)

    if teardown is not None:
        targets = _service_targets(config, teardown)
        if not targets:
            pytest.exit("ducktest --teardown-service: no matching declared service(s)", returncode=1)
        for _, svc in targets:
            if svc.stop is not None:
                with step(f"tearing down service {svc.key}"):
                    svc.stop(config)
            print(f"✓ {svc.key}: stopped")
        pytest.exit(f"ducktest: torn down {len(targets)} service(s)", returncode=0)

    targets = _service_targets(config, prov)
    if not targets:
        pytest.exit("ducktest --provision-service: no matching declared service(s)", returncode=1)
    for _, svc in targets:
        probe = svc.attach({}, config) if svc.attach is not None else {}
        if svc.start is None:
            # invocation-external (phase 5): no boot capability at all, it just permanently exists --
            # nothing for this command to do beyond reporting the attach line.
            print(f"✓ {svc.key}: invocation-external (no start) — nothing to provision")
            block = probe
        elif svc.alive is not None and svc.alive(probe):
            print(f"✓ {svc.key}: already running at {probe.get('endpoint', '<default>')} (skipped)")
            block = probe
        else:
            started = svc.start(config)
            block = started if isinstance(started, dict) else probe
            print(f"✓ {svc.key}: up at {block.get('endpoint', '<started>')}")
        env_key = svc.key.upper().replace("-", "_")
        ep = block.get("endpoint", "")
        print(f"    attach: --existing-service {svc.key}={ep}   (or env DUCKTEST_EXISTING_SERVICE_{env_key}={ep})")
    pytest.exit(f"ducktest: provisioned {len(targets)} service(s); left running", returncode=0)


# ---------------------------------------------------------------------------
# Suite membership + Phase-1 selection: auto-marker + default-scan deselection
# ---------------------------------------------------------------------------


def _no_explicit_selection(config):
    """True iff the invocation gave no path arg, no ``-k``, and no ``-m`` (a bare run)."""
    opt = config.option
    return not (
        list(getattr(opt, "file_or_dir", None) or [])
        or (getattr(opt, "keyword", None) or "")
        or (getattr(opt, "markexpr", None) or "")
    )


def _is_subpath(child, parent):
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def _item_in_suite_path(config, item, suite):
    """Whether ``item``'s file is at/under the suite's repo-relative ``path``."""
    if not suite.path:
        return False
    tabs = os.path.normpath(os.path.join(_working_dir(config), suite.path))
    ipath = os.path.normpath(str(getattr(item, "path", "") or ""))
    return _is_subpath(ipath, tabs)


def _item_in_suite(config, item, suite):
    """Whether ``item`` belongs to ``suite`` — by the suite's marker or by path membership."""
    if suite.marker and item.get_closest_marker(suite.marker) is not None:
        return True
    return _item_in_suite_path(config, item, suite)


# ---------------------------------------------------------------------------
# Suite-level matrix fan-out (RESOURCE-PLANNING.md §5 phase 9): a suite's `matrix=` elevates
# `@requires_matrix`'s per-test cell fan-out to "every member of this suite runs across these
# cells", for BOTH `.py` (pytest_generate_tests, collection-time parametrize) and `.test`
# (a post-collection list-splice — SqlLogicFile.collect() stays a plain single yield).
# ---------------------------------------------------------------------------


def _has_own_matrix_parametrize(metafunc) -> bool:
    """True iff the test function already carries its own `@requires_matrix`-style parametrize
    over `matrix_cell` (explicit wins over the suite's implicit matrix — see the plan's
    Composition question: today's suite-membership is coarse path/marker-only, with no
    include/exclude to resolve a conflict declaratively, so explicit-wins is the pragmatic default)."""
    for mark in metafunc.definition.iter_markers(name="parametrize"):
        if mark.args and mark.args[0] == "matrix_cell":
            return True
    return False


def pytest_generate_tests(metafunc):
    """`.py` suite-matrix fan-out: a test that pulls in `matrix_cell` (directly, or transitively via
    `resources`) gets parametrized over its suite's `matrix=` cells — the SAME indirect-fixture
    plumbing `@requires_matrix` already established (plugin.py's `matrix_cell` fixture), so no new
    consumption-side code is needed. A test with its own `@requires_matrix` is left alone (explicit
    wins), with a verbose-mode note so the override isn't silent."""
    if "matrix_cell" not in metafunc.fixturenames:
        return
    config = metafunc.config
    suites = [s for s in get_suites(config) if s.matrix]
    if not suites:
        return
    suite = next((s for s in suites if _item_in_suite(config, metafunc.definition, s)), None)
    if suite is None:
        return
    if _has_own_matrix_parametrize(metafunc):
        if config.option.verbose > 0:
            warnings.warn(
                f"{metafunc.definition.nodeid}: has its own @requires_matrix AND belongs to suite "
                f"{suite.name!r} (matrix=...) — the test's own matrix wins; the suite's is not applied."
            )
        return
    metafunc.parametrize(
        "matrix_cell",
        [pytest.param(cell, id=cell["backend"]) for cell in suite.matrix],
        indirect=True,
    )


def _expand_test_matrix(config, items, suites):
    """`.test` suite-matrix fan-out: replace each `SqlLogicItem` belonging to a `matrix=` suite with
    one sibling per cell, stamping `_cell` (feeds `decorate()`'s `Key.cell`, so `_batch_key` never
    batches two cells of the same file together — impossible anyway, since Catch2's registered test
    identity IS the file path). Runs BEFORE dedup/`assign_batches` so batching sees the full,
    cell-expanded set. Non-SqlLogic items and items whose suite declares no matrix pass through
    untouched — a no-op for every suite that doesn't use this.
    """
    matrix_suites = [s for s in suites if s.matrix]
    if not matrix_suites:
        return items
    expanded = []
    for item in items:
        if not isinstance(item, SqlLogicItem):
            expanded.append(item)
            continue
        suite = next((s for s in matrix_suites if _item_in_suite(config, item, s)), None)
        if suite is None:
            expanded.append(item)
            continue
        for cell in suite.matrix:
            sibling = _new_item(
                item.parent,
                name=f"{item.name}[{cell['backend']}]",
                test_name=item._test_name,
                binary=item._binary,
                working_dir=item._working_dir,
                temp_roots=item._temp_roots,
            )
            sibling._cell = cell["backend"]
            sibling._matrix_cell = cell  # full cell dict, for a backend conftest that wants more
            expanded.append(sibling)
    return expanded


def _apply_suite_markers(config, items, suites):
    """Stamp each suite's marker on every item that belongs to it BY PATH (the auto-marker).

    Makes `-m <suite>` work for path-declared members — including SQLLogic bodies that can't carry a
    Python `@pytest.mark`. Runs pre-yield so the marks exist before pytest's own mark deselection.
    """
    for suite in suites:
        if not suite.marker:
            continue
        for item in items:
            if _item_in_suite_path(config, item, suite) and item.get_closest_marker(suite.marker) is None:
                item.add_marker(suite.marker)


def apply_suite_markers(ctx, session):
    """Public entry the controller calls: stamp suite auto-markers onto the session's items.

    (Called before the controller's `perform_collect`, where `session.items` is still empty, AND — the
    load-bearing pass — from the collection hookwrapper below, pre-yield, once items exist. Idempotent.)
    """
    config = session.config
    _apply_suite_markers(config, list(session.items), get_suites(config))


def _default_scan_deselect(config, items, suites):
    """On a bare run, deselect items in a non-default suite — the explicit default scan.

    Only fires when no explicit selection was given; any `-m`/`-k`/path is respected verbatim. An item
    that also belongs to a default suite stays. Removal is the pytest-standard `pytest_deselected`.
    """
    if not _no_explicit_selection(config):
        return
    non_default = [t for t in suites if not t.default]
    if not non_default:
        return
    default_suites = [t for t in suites if t.default]
    removed, kept = [], []
    for item in items:
        in_out = any(_item_in_suite(config, item, t) for t in non_default)
        in_keep = any(_item_in_suite(config, item, t) for t in default_suites)
        (removed if in_out and not in_keep else kept).append(item)
    if removed:
        config.hook.pytest_deselected(items=removed)
        items[:] = kept


def _deselected_suite_names(config):
    """Names of the non-default suites a bare run deselects (from args alone; for the banner)."""
    if not _no_explicit_selection(config):
        return []
    return [t.name for t in get_suites(config) if not t.default]


def _suite_banner(config):
    """The mandatory 'default set selected; deselected: …' banner line, or None when none applies."""
    names = _deselected_suite_names(config)
    if not names:
        return None
    listed = ", ".join(sorted(names))
    hint = names[0] if len(names) == 1 else "<suite>"
    return f"duck-test suites: default set selected; deselected: {listed} (pass a path or -m {hint} to include)"


# ---------------------------------------------------------------------------
# step() narration
# ---------------------------------------------------------------------------


def _narrating(config):
    """Whether ``step()`` narration should be surfaced live: ``--steps``, or a ``--repl`` /
    ``--provision-keep`` session unless ``--no-steps`` (``--steps`` always wins)."""
    steps = config.getoption("--steps", default=False)
    repl_like = config.getoption("--repl", default=False) or config.getoption("--provision-keep", default=False)
    return bool(steps or (repl_like and not config.getoption("--no-steps", default=False)))


@contextlib.contextmanager
def _narrate_driver_log(config):
    """Surface the ``driver`` logger's INFO ``step()`` output live during configure-time provisioning."""
    if not _narrating(config):
        yield
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    driver_log = logging.getLogger("driver")
    driver_log.addHandler(handler)
    capman = config.pluginmanager.getplugin("capturemanager")
    try:
        if capman is not None:
            with capman.global_and_fixture_disabled():
                yield
        else:
            yield
    finally:
        driver_log.removeHandler(handler)


# ---------------------------------------------------------------------------
# pytest_configure: build the SessionContext + session-scope setup
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Resolve + cache the working dir + the test-local import roots up front.
    _working_dir(config)
    _ensure_pythonpath(config)

    # Build the ONE typed SessionContext (replaces the config._duckdb_* string-attribute sprawl) and
    # stash it on pytest's typed stash. Binary resolution stays lazy (resolved on first `.test` need),
    # so a no-binary offline / pure-Python run doesn't fail at configure.
    ctx = SessionContext(
        working_dir=_working_dir(config),
        run_id=_run_id(config),
        options={
            "existing_services": _parse_existing_services(
                config.getoption("--existing-service", default=[]) or [], os.environ
            ),
            "collect_source": config.getoption("--collect-source", default="verify"),
        },
    )
    set_context(config, ctx)

    # Register the @requires marker so it doesn't warn under --strict-markers.
    config.addinivalue_line(
        "markers",
        "requires(source, access, properties, name): declare an external resource need; drives "
        "provisioning under --repl (see ducktest/requires.py).",
    )

    # Redirect an explicitly-named `.test` arg to its same-stem driver `.py` (the body is suppressed).
    rewritten = []
    for arg in config.args:
        if arg.endswith(".test") and os.path.exists(arg):
            driver = arg[: -len(".test")] + ".py"
            if os.path.exists(driver):
                rewritten.append(driver)
                continue
        rewritten.append(arg)
    config.args[:] = rewritten

    # --repl and --steps must run single-process; force it before pytest-xdist reads numprocesses.
    if config.getoption("--repl", default=False) or config.getoption("--steps", default=False):
        if getattr(config.option, "numprocesses", None):
            config.option.numprocesses = 0
        if getattr(config.option, "dist", "no") not in (None, "no"):
            config.option.dist = "no"

    # Emit driver step() records at INFO so they're captured into a failing test's report.
    logging.getLogger("driver").setLevel(logging.INFO)
    if _narrating(config):
        if config.getoption("--log-cli-level", default=None) is None:
            config.option.log_cli_level = "INFO"


def pytest_sessionfinish(session, exitstatus):
    # Controller-only: stop store-present services + shut the store down, then apply the temp-dir policy.
    config = session.config
    if getattr(config, "workerinput", None) is not None:
        return  # this is a worker
    _teardown_store(config)
    # REMOTE sweeping (SPEC §11.5), controller-only + once: the driver does NOTHING local (the binary owns
    # LOCAL create/sweep, as-it-goes — it received --temp-dir-base/--temp-dir-run-id/--temp-dir-destroy).
    # Here it applies the one session-end remote sweep (keep-on-failure by keep-list) then the best-effort
    # age-sweep backstop. Both no-op when <root> is local / no sweeper is registered. A run that never
    # reaches sessionfinish does no sweep — the age-sweep is the eventual backstop (SPEC §11.5).
    from .sweeper import age_sweep, sweep_session

    sweep_session(config)
    age_sweep(config)


# ---------------------------------------------------------------------------
# Members, roles, and drivers
# ---------------------------------------------------------------------------


def _stem_path(path, suffix):
    return os.path.splitext(str(path))[0] + suffix


def run_paired(request, *, env=None):
    """Drive the calling driver `.py`'s same-stem body (`.test`) through the binary.

    Call from the driver's test function once initialization has run. Raises SqlLogicFailure on failure
    and pytest.skip on a skipped test. Pass ``env`` (e.g. a provisioning fixture's ``bindings.env``) to
    inject vars the body substitutes via ``${...}`` (merged over os.environ).

    The run's originated TEMP/DATA roots (SPEC §11.2) are attached automatically — this invocation
    shares the run's `<root>/<session-id>` and composes its own `<batch-id>` from the body name (+ its
    matrix cell, if any — a `@requires_matrix`/suite-`matrix=`-parametrized paired driver calls
    `run_paired` once per cell, all with the identical body name; folding the cell in keeps each
    cell's temp-dir path distinct), never re-derived here.
    """
    working_dir = request.config.sqllogic_working_dir
    binary = find_binary(request.config, working_dir)
    test_path = _stem_path(request.path, ".test")
    test_name = os.path.relpath(test_path, working_dir)
    cell = request.getfixturevalue("matrix_cell")
    with step(f"running {test_name}"):
        _raise_for_result(
            _parse_result(
                _invoke(
                    binary,
                    [test_name],
                    working_dir,
                    _temp_roots(request.config),
                    batch_id=_test_batch_id(test_name + _cell_suffix(cell)),
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
    """The current `@requires_matrix` cell (its concrete properties dict); `None` for a non-matrix test."""
    return getattr(request, "param", None)


@pytest.fixture
def resources(request, matrix_cell):  # matrix_cell: closure hook for indirect @requires_matrix
    """Provision a test's @requires fixtures, yield the bindings, tear down after.

    The generic run-path counterpart of --repl (same provisioner). CONSUMES already-provisioned suite
    resources; it does not provision suite-level services itself (those came from the collect-first
    plan). No-ops (empty env) for tests without @requires.
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
            "(the backend conftest must call ducktest.register_provisioner)."
        )
    token = _provision_token(request.config, request.node)
    bindings = provisioner.provision(specs, token, params=_item_params(request.node), config=request.config)
    try:
        yield bindings
    finally:
        provisioner.teardown(bindings=bindings)


# ---------------------------------------------------------------------------
# --repl: provision (via @requires, and/or the selected suites' services/credentials) ->
# interactive duckdb -> teardown
# ---------------------------------------------------------------------------


def pytest_collection_finish(session):
    config = session.config
    if not config.getoption("--repl", default=False):
        return
    if getattr(config, "workerinput", None) is not None:
        return  # xdist workers also reach here; only the controller drives the shell
    _shell_provision_flow(session, config)


def _item_params(item):
    cs = getattr(item, "callspec", None)
    return dict(cs.params) if cs is not None else {}


def _shell_provision_flow(session, config):
    from .requires import collect_requirements
    from .provision import get_provisioner

    dry_run = config.getoption("--provision-dry-run", default=False)
    keep = config.getoption("--provision-keep", default=False)

    items = list(session.items)
    if len(items) != 1:
        raise pytest.UsageError(
            f"--repl requires exactly ONE selected test, but {len(items)} were collected. "
            "Narrow the selection (pass a single driver .py / use -k)."
        )
    item = items[0]
    specs = collect_requirements(item)
    provisioner = get_provisioner(config, item.path)

    if provisioner is None:
        if specs:
            raise pytest.UsageError(
                f"--repl: {item.nodeid!r} declares @requires but no provisioner is registered to "
                "satisfy them (the backend conftest must call ducktest.register_provisioner(...))."
            )
        print()
        print("=" * 70)
        print(f"--repl for test: {item.nodeid}")
        print("no @requires + no provisioner -> launching a duckdb REPL")
        print("=" * 70)
        if dry_run:
            init_sql = _repl_resource_init_sql(config, session, redact=True)
            if init_sql:
                print()
                print("----- would-be duckdb init SQL (secrets redacted) -----")
                print(init_sql)
                print("-------------------------------------------------------")
            print()
            pytest.exit("--repl --provision-dry-run: nothing to provision; shell NOT launched.", returncode=0)
        _launch_shell(config, _repl_resource_init_sql(config, session))
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
            print(f"  [{i}] source={s.source} access={s.access} properties={s.properties} name={s.resolved_name()}")
    else:
        print("no @requires -> minimal provision (REPL only)")
    print("=" * 70)

    if dry_run:
        bindings = provisioner.provision(specs, token, dry_run=True, params=_item_params(item), config=config)
        print()
        print("----- would-be duckdb init SQL (secrets redacted) -----")
        print(_repl_resource_init_sql(config, session, redact=True) + provisioner.make_init_sql(bindings, redact=True))
        print("-------------------------------------------------------")
        print()
        print("--provision-dry-run: NO DDL executed, shell NOT launched, NO teardown.")
        pytest.exit("--repl --provision-dry-run complete", returncode=0)
        return

    bindings = provisioner.provision(specs, token, dry_run=False, params=_item_params(item), config=config)
    try:
        init_sql = _repl_resource_init_sql(config, session) + provisioner.make_init_sql(bindings)
        _launch_shell(config, init_sql)
    finally:
        if keep:
            print()
            print(f"--provision-keep: leaving fixtures (token={token}) in place.")
            print("Tear them down later via the backend's clean tool / teardown(bindings).")
        else:
            with step(f"tearing down provisioned fixtures (token={token})"):
                provisioner.teardown(bindings=bindings)
    pytest.exit("--repl session complete", returncode=0)


def _provision_token(config, node=None):
    """SQL-safe per-invocation id for cell-schema names: ``<YYYYMMDD>_<mnemonic>`` (+ a short nodeid
    hash when ``node`` is given, so each test gets a unique, xdist-stable token)."""
    rid = _run_id(config)
    ts, _, mnem = rid.partition("--")
    mnem = mnem.replace("-", "_")
    date = ts.split("T", 1)[0].replace("-", "")
    token = f"{date}_{mnem}"
    if node is None:
        return token
    import hashlib

    return f"{token}_{hashlib.sha1(node.nodeid.encode()).hexdigest()[:6]}"


def _launch_shell(config, init_sql):
    """Write init_sql to a temp file and exec an interactive `duckdb -unsigned -init`."""
    working_dir = getattr(config, "sqllogic_working_dir", os.getcwd())
    binary = find_binary(config, working_dir)
    build_dir = os.path.dirname(os.path.dirname(binary))
    duckdb_bin = os.path.join(build_dir, "duckdb")
    if not os.path.isfile(duckdb_bin):
        raise pytest.UsageError(f"--repl: duckdb shell not found at {duckdb_bin}. Build it (e.g. make release).")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", prefix="shell_init.", delete=False) as f:
        f.write(init_sql)
        init_path = f.name
    capman = config.pluginmanager.getplugin("capturemanager")
    cmd = [duckdb_bin, "-unsigned", "-init", init_path]

    def _run():
        try:
            tty = open("/dev/tty", "r+b", buffering=0)
        except OSError:
            subprocess.run(cmd, cwd=working_dir)
            return
        try:
            subprocess.run(cmd, cwd=working_dir, stdin=tty, stdout=tty, stderr=tty)
        finally:
            tty.close()

    try:
        with step("launching interactive duckdb shell (exit to continue)"):
            if capman is not None:
                with capman.global_and_fixture_disabled():
                    _run()
            else:
                _run()
    finally:
        os.unlink(init_path)


# ---------------------------------------------------------------------------
# Collection modifyitems: auto-marker + default-scan deselect (pre-yield), then dedup + batch +
# worker-side up-front provisioning (post-yield).
# ---------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(session, config, items):
    suites = get_suites(config)
    if suites:
        # Register each suite's marker so the auto-applied mark doesn't warn and shows in --markers.
        for suite in suites:
            if suite.marker:
                config.addinivalue_line("markers", f"{suite.marker}: ducktest suite {suite.name!r}")
        # PRE-yield: stamp auto-markers + default-scan deselect BEFORE pytest's builtin -m/-k deselection
        # (a plain impl) reads them — the whole trick for `-m cloud` selecting a marker-less body.
        _apply_suite_markers(config, items, suites)
        _default_scan_deselect(config, items, suites)
        # `.test` suite-matrix fan-out (§ phase 9): splice cell siblings in for the survivors of
        # selection, THEN re-stamp suite markers so the new sibling items carry them too (a `-m
        # <suite>` deselection reads markers, which must exist before this hookwrapper yields).
        items[:] = _expand_test_matrix(config, items, suites)
        _apply_suite_markers(config, items, suites)

    # Dedupe by nodeid, then batch — both pre-yield: assign_batches' xdist_group marker must exist before
    # xdist's plain pytest_collection_modifyitems (remote.py) appends the `@group` nodeid suffix that
    # `--dist=loadgroup` reads to keep a batch on one worker. Stamping post-yield (after our hookwrapper's
    # yield) runs too late — loadgroup then scatters a batch across workers, each re-running the whole
    # batch on the same `<session-id>/<batch-id>` temp dir concurrently.
    seen = set()
    deduped = []
    for it in items:
        if it.nodeid in seen:
            continue
        seen.add(it.nodeid)
        deduped.append(it)
    items[:] = deduped

    batch_size = config.getoption("--batch-size", default=10)
    n_batches = assign_batches(items, batch_size=batch_size) if batch_size > 1 else 0

    # Batching only groups correctly under --dist=loadgroup (it honors the xdist_group mark); any other
    # parallel scheduler scatters a batch across workers, and each re-runs the whole batch on the same
    # temp dir (IO/lock corruption). Fail loud instead. Gated on real batches (n_batches) so a pure-
    # Python parallel run — no .test items — isn't blocked; serial (-n0) is one process, so it's safe.
    if n_batches and getattr(config.option, "numprocesses", None):
        dist = getattr(config.option, "dist", "no")
        if dist != "loadgroup":
            raise pytest.UsageError(
                f"--batch-size {batch_size} needs --dist=loadgroup for a parallel (-n) run, but got "
                f"--dist={dist!r}. loadgroup keeps each batch on one worker; any other scheduler scatters "
                f"a batch across workers and corrupts its shared temp dir. Pass --dist=loadgroup, or set "
                f"--batch-size 1 to disable batching."
            )

    yield

    # Selection is final; trim each batch's member list to survivors so a -k/-m run doesn't execute
    # excluded tests in the single -f invocation (the marker + `_batch_id` stay — batch already grouped).
    survivors: dict = {}
    for it in items:
        bid = getattr(it, "_batch_id", None)
        if bid is not None:
            survivors.setdefault(bid, []).append(it._test_name)
    for it in items:
        if getattr(it, "_batch_id", None) is not None:
            it._batch_test_names = survivors[it._batch_id]

    # Workers adopt the plan's env FROM THE STORE (SPEC §3.7): provision what their real selection needs
    # (creds + services), single-flighted so the controller's up-front boot/fetch isn't duplicated. The
    # controller itself provisions from the collect-first Plan (see controller.py), so gate on worker.
    if getattr(config, "workerinput", None) is not None and suites:
        provision_reachable(config, _reachable_suites(config, items))


# ---------------------------------------------------------------------------
# Terminal summary: aggregate skip reasons across the whole run
# ---------------------------------------------------------------------------


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Consolidate per-test skip reasons into one counted digest for the run."""
    from collections import Counter

    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    counts = Counter()
    for rep in skipped:
        longrepr = getattr(rep, "longrepr", None)
        reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
        counts[reason.removeprefix("Skipped: ")] += 1

    terminalreporter.write_sep("-", f"skipped: {len(skipped)} by reason", yellow=True, bold=True)
    for reason, n in counts.most_common():
        terminalreporter.write_line(f"  {n:>4}  {reason}", yellow=True)
