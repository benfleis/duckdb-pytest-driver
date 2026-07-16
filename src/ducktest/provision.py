"""Generic provisioner protocol + registry (extension-AGNOSTIC).

The framework knows `@requires` (driver/requires.py) but not how to satisfy it on
a given backend. A backend supplies a *Provisioner*: three callables the `--repl`
flow dispatches through. This keeps the framework portable — it never imports a
backend; the backend registers itself.

PROTOCOL — a Provisioner is any object exposing (duck-typed; a backend need not subclass
the `Provisioner` base below, though it's the easiest way to get this protocol right):

    provision(specs, token, *, dry_run) -> bindings
        specs : list[Requirement] from @requires on the selected test.
        token : SQL-safe per-invocation id (the cell-schema suffix).
        dry_run : if True, resolve + return the plan WITHOUT executing DDL.
        returns an opaque `bindings` object the backend's make_init_sql understands.

    make_init_sql(bindings, *, redact=False) -> str
        Concrete duckdb init SQL. Secrets baked for the real launch; redact=True
        (used by --provision-dry-run, which prints it) masks them for display.

    teardown(bindings=None) -> None
        Release/destroy what provision() created. `bindings.token` carries what a
        separate `token` arg used to (dropped as redundant).

BASE CLASS — `Provisioner` below implements the generic access-policy spec-loop (rw ->
isolated namespace + instantiate + track-for-teardown; ro -> shared target, instantiate
once per session) + a default `teardown()`; a backend subclasses it and implements the
hooks (`execute`, `rw_target`, `ro_target`, `instantiate`, `make_init_sql`, required;
`env_for`/`drop_sql`/`new_bindings`/`dry_run_summary` have workable defaults). See
`uc.databricks.engine.DatabricksProvisioner` for a real implementation.

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
from dataclasses import dataclass, field

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


# ---------------------------------------------------------------------------
# Base Provisioner + Bindings — the generic core every backend re-implemented
# ---------------------------------------------------------------------------
#
# UC (OSS + Databricks) each hand-rolled the same shape: a per-spec access-policy loop
# (rw -> isolated namespace + instantiate + track-for-teardown; ro -> shared target,
# instantiate ONCE per session), a dry-run plan, and a teardown that drops what rw
# created. This slides that generic core down here; a backend subclasses `Provisioner`
# and implements the hooks below. See `uc.databricks.engine.DatabricksProvisioner` for
# a real implementation, and driver/docs/ARCHITECTURE.md § *Provisioning* / PLAN.md § *Base
# Provisioner* for the design this came from (including naming decided in UC's
# WIP-identity-design.md: `make_init_sql` not `make_init`; `teardown(bindings)` — no
# separate `token` arg, it's already `bindings.token`).


@dataclass
class Bindings:
    """Generic result of `Provisioner.provision()` — what teardown/env assembly need.

    token          : the provision token (isolation-namespace suffix).
    catalog        : the backend's catalog/root, if it has one — None for a backend with
                     no catalog concept (e.g. a bare-path source; not every backend is
                     catalog-shaped, don't assume it).
    default_schema : the schema/namespace a run path ATTACHes/USEs by default, if
                     applicable (None if N/A).
    tables         : per-requirement binding records — shape is backend-defined
                     (e.g. a `TableBinding`/`TableRef`); the base never reads or writes
                     this list, only a backend's `instantiate()`/hooks do.
    isolated       : namespaces created for `rw` specs (via `ensure_isolated`), torn down
                     by the base `teardown()`.
    env            : the env dict a run path adopts (`${VAR}` substitution in the body).
    plan           : human-readable dry-run plan lines.
    """

    token: str
    catalog: str = None
    default_schema: str = None
    tables: list = field(default_factory=list)
    isolated: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    plan: list = field(default_factory=list)


class Provisioner:
    """Base Provisioner: the generic access-policy spec-loop + RO once-guard + teardown.

    `provision()`/`teardown()` are provided; a backend overrides the hooks below (only
    `execute`, `rw_target`, `ro_target`, `instantiate`, and `make_init_sql` are required —
    the rest have workable defaults). None of this assumes a service already booted via a
    fixture pull — a backend's hooks may resolve their connection however they need to
    (a session fixture, a suite-level env-adopted service, whatever), the base doesn't care.
    """

    def __init__(self):
        # RO targets instantiated once per session (per worker under xdist); guards
        # re-instantiation on a later spec that references the same shared target.
        self._shared_ro = set()

    def provision(self, specs, token, *, dry_run=False, params=None) -> Bindings:
        """Provision `specs` under `token`. `dry_run=True` builds the plan/bindings but
        executes no DDL (each hook is expected to honor `dry_run` the same way)."""
        self.before_provision(specs, token, dry_run)
        bindings = self.new_bindings(token, params=params)
        for spec in specs:
            if spec.access == "rw":
                target = self.rw_target(spec, token, bindings, dry_run)
                self.instantiate(spec, target, dry_run, bindings)
            else:
                target = self.ro_target(spec, bindings)
                if target in self._shared_ro:
                    bindings.plan.append(f"[ro] {target} already provisioned this session")
                else:
                    self.instantiate(spec, target, dry_run, bindings)
                    if not dry_run:
                        self._shared_ro.add(target)
        self.finalize_bindings(bindings)
        bindings.env = self.env_for(bindings)
        if dry_run:
            print("provision plan (NO DDL executed):")
            for line in bindings.plan:
                print(f"  {line}")
            self.dry_run_summary(bindings)
        return bindings

    def teardown(self, bindings=None) -> None:
        """Drop each namespace `rw_target`/`ensure_isolated` tracked in `bindings.isolated`."""
        isolated = list(bindings.isolated) if bindings else []
        if not isolated:
            tok = bindings.token if bindings else "?"
            print(f"teardown: nothing to drop for token={tok}")
            return
        for ns in isolated:
            self.execute(self.drop_sql(ns))

    def ensure_isolated(self, namespace, bindings, dry_run, *, create_sql=None):
        """Idempotently track + create one `rw` namespace — call from `rw_target()`.

        No-ops if `namespace` is already tracked (a second `rw` spec in the same cell).
        `create_sql` overrides the default `CREATE SCHEMA IF NOT EXISTS` DDL.
        """
        if namespace in bindings.isolated:
            return
        bindings.isolated.append(namespace)
        sql = create_sql if create_sql is not None else f"CREATE SCHEMA IF NOT EXISTS {namespace}"
        bindings.plan.append(f"{sql};")
        if not dry_run:
            self.execute(sql)

    # -- hooks a backend implements --

    def before_provision(self, specs, token, dry_run):
        """Optional upfront validation/env-setup before any spec is provisioned (e.g. a
        `--repl`-with-no-`@requires` guard, a credential-availability check). Default: no-op."""

    def new_bindings(self, token, *, params=None) -> Bindings:
        """Construct the (possibly backend-populated) `Bindings` for this provision() call.
        Default: bare `Bindings(token=token)`; override to pre-set `catalog`/`default_schema`
        from env/config, or to return a `Bindings` subclass with extra fields."""
        return Bindings(token=token)

    def execute(self, sql):
        """Run one DDL/DML statement. Required."""
        raise NotImplementedError

    def rw_target(self, spec, token, bindings, dry_run):
        """The isolated target for an `rw` spec (call `ensure_isolated(ns, bindings, dry_run)`
        for its namespace, if any). Required."""
        raise NotImplementedError

    def ro_target(self, spec, bindings):
        """The shared target for an `ro` spec. Required."""
        raise NotImplementedError

    def instantiate(self, spec, target, dry_run, bindings):
        """Seed `target` from `spec`'s source (a `TableSpec` or a backend-native def);
        append whatever binding record this backend wants to `bindings.tables`. Required."""
        raise NotImplementedError

    def finalize_bindings(self, bindings):
        """Called once after the spec loop, before `env_for` — e.g. reconciling
        `bindings.default_schema` from per-spec bookkeeping a backend tracked itself
        during `rw_target`/`ro_target` (the base doesn't track that; it's backend-shaped).
        Default: no-op."""

    def env_for(self, bindings) -> dict:
        """The env dict for `bindings.env`. Default: empty (no env injected)."""
        return {}

    def drop_sql(self, namespace) -> str:
        """DDL to tear down one isolated namespace. Default: `DROP SCHEMA ... CASCADE`."""
        return f"DROP SCHEMA IF EXISTS {namespace} CASCADE"

    def dry_run_summary(self, bindings):
        """Optional extra dry-run print after the plan (e.g. cell schemas, DEFAULT_SCHEMA).
        Default: no-op."""

    def make_init_sql(self, bindings, *, redact: bool = False) -> str:
        """Concrete duckdb init SQL for `duckdb -unsigned -init` (the `--repl` launch).
        `redact=True` (used by `--provision-dry-run`, which prints this without launching)
        must not require a built binary and must mask secrets. Required."""
        raise NotImplementedError
