"""Generic provisioner protocol + registry (extension-AGNOSTIC).

The framework knows `@requires` (driver/requires.py) but not how to satisfy it on
a given backend. A backend supplies a *Provisioner*: three callables the `--repl`
flow dispatches through. This keeps the framework portable — it never imports a
backend; the backend registers itself.

PROTOCOL — a Provisioner is any object exposing:

    provision(specs, token, *, dry_run) -> bindings
        specs : list[Requirement] from @requires on the selected test.
        token : SQL-safe per-invocation id (the cell-schema suffix).
        dry_run : if True, resolve + return the plan WITHOUT executing DDL.
        returns an opaque `bindings` object the backend's make_init understands.

    make_init(bindings, *, redact=False) -> str
        Concrete duckdb init SQL. Secrets baked for the real launch; redact=True
        (used by --provision-dry-run, which prints it) masks them for display.

    teardown(token, bindings=None) -> None
        Release/destroy what provision() created.

REGISTRATION SEAM (the architecture decision):
    A backend registers from the conftest that scopes it (e.g.
    test/sql/databricks/conftest.py) via:

        from ducktest import register_provisioner
        def pytest_configure(config):
            register_provisioner(config, MyProvisioner())

    Registering from a subtree conftest means the provisioner is present only when
    that subtree's tests are collected — resolution is by TEST LOCATION, not a
    hardcoded backend. Stored on `config` (the same idiom as
    `config.sqllogic_working_dir`); last registration wins, which is the natural
    behavior since only the relevant subtree's conftest runs for a given selection.
"""

import os

_ATTR = "_driver_provisioners"  # list[(scope_dir|None, provisioner)]


def register_provisioner(config, provisioner, scope=None):
    """Register a backend provisioner, scoped to a directory (call from a conftest).

    `scope` is the registering conftest's dir (pass `os.path.dirname(__file__)` or
    `pathlib.Path(__file__).parent`). Resolution is by TEST LOCATION:
    `get_provisioner(config, path)` returns the provisioner whose scope is the nearest
    ancestor of `path`. This is what makes a MIXED selection correct -- tests from >1
    backend subtree in one run (e.g. oss_local + databricks) each resolve to THEIR
    backend. A single global last-wins registration would hand one backend's tests the
    other's provisioner. `scope=None` registers a global fallback (matches any test no
    scoped provisioner claims). See module docstring for the protocol.
    """
    regs = getattr(config, _ATTR, None)
    if regs is None:
        regs = []
        setattr(config, _ATTR, regs)
    regs.append((os.path.abspath(str(scope)) if scope is not None else None, provisioner))


def get_provisioner(config, path=None):
    """Return the provisioner for `path` (nearest-ancestor scope), else the global
    (scope=None) one, else None. With no `path`, returns the most-recently registered
    (back-compat for callers that don't have an item)."""
    regs = getattr(config, _ATTR, None)
    if not regs:
        return None
    if path is None:
        return regs[-1][1]
    p = os.path.abspath(str(path))
    best, best_len, fallback = None, -1, None
    for scope, prov in regs:
        if scope is None:
            fallback = prov
        elif (p == scope or p.startswith(scope + os.sep)) and len(scope) > best_len:
            best, best_len = prov, len(scope)
    return best if best is not None else fallback
