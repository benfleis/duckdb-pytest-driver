"""Test-suite declaration API + registry (Phase 0: inert registration only).

A **suite** is a named subset of the tests with a default-selection policy and a set of
up-front resources (see ``docs/ARCHITECTURE.md``). This module owns the *declaration* half:
frozen descriptors (``Credential``, ``Service``, ``Suite``) plus ``register_suite`` /
``get_suites``, mirroring the ``register_broadcast`` / ``register_provisioner`` idiom of
stashing config-scoped state on ``config``.

**Phase 0 is behavior-free.** Nothing here fetches a credential, starts a service,
deselects an item, or adds a pytest hook — it only records the callables so later phases
(selection, up-front fetch, service gating) can act on them. Two orthogonal resource
classes ride on a suite:

- **class-1 credential** — fetched once, up front, on the controller, then broadcast to
  workers. ``credential(key, fetch=..., validate=..., error=..., adopt=...)``.
- **class-2 service** — provisioned up front, from the collect-first plan, on the controller,
  and torn down once by the controller. ``service(key, start=..., stop=..., fixture=...)``.

**No per-service "when".** In the redesign every service a *selected* test needs is provisioned
UP FRONT from the collect-first plan on the controller — there is no disposition to choose, so the
old ``eager`` vs ``on_demand`` fork (and its ``provision`` field / ``PROVISION`` vocabulary) is gone.
``to_env`` (per-process env) and ``populate`` (shared-service state) stay — they are WHAT/HOW, not
WHEN, and remain orthogonal to the removed disposition.

The descriptors just *hold* these callables; they are dumb by design (same portability
stance as ``@requires`` / the provisioner protocol — the framework never imports a
backend). Declare from a repo's ``test/conftest.py`` ``pytest_configure``::

    from ducktest import register_suite, credential, service

    def pytest_configure(config):
        register_suite(config, "databricks", path="test/databricks", default=False,
            credentials=[credential("databricks_creds", fetch=load_creds,
                         validate=have_core_creds, error=cred_failure_detail, adopt="env")])
"""

from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

# Config attribute holding the ordered list of registered Suites (mirrors
# `_duckdb_broadcast_factories` / `_driver_provisioners`). Created lazily.
_ATTR = "_duckdb_suites"


@dataclass(frozen=True)
class Credential:
    """A class-1 credential descriptor — fetched once, up front, on the controller.

    Fields (all just *held* in Phase 0; nothing is called):
      key      : broadcast key (also the ``register_broadcast`` key in later phases).
      fetch    : ``fetch(config) -> dict|obj``; runs once on the controller (matches UC's
                 ``load_creds`` signature). The lone required callable.
      validate : ``validate(value) -> bool`` feeding the up-front hard-fail; None => always
                 considered valid.
      error    : ``error() -> str`` (or ``error(value) -> str``) supplying the failure
                 message when ``validate`` is false; None => a generic message later.
      adopt    : ``"env"`` => ``os.environ.update(value)`` on every process that receives
                 the value; None => the value is broadcast but not adopted into the env.
      available: ``available() -> bool`` -- a NON-INTERACTIVE check of whether the credential is
                 already usable in this process's environment (e.g. preset env vars), independent of
                 the store. The per-test backstop consults it so a ``-k``-selected live test with
                 creds already in the env passes without an (op-prompting) fetch; None => the backstop
                 checks only the store.
      late_fetch: default True -- when a selected test's credential is absent up front AND not
                 ``available()`` in env, the backstop performs a LATE ``fetch`` (single-flighted across
                 workers via the store, so at most one interactive prompt), rescuing e.g. a
                 ``-k``-selected run. Set False for strict "fail fast, never prompt mid-run" (CI).
      to_init_sql: ``to_init_sql(value, *, redact=False) -> str`` -- SQL fed into a ``--repl`` session's
                 ``-init`` file (e.g. a ``CREATE SECRET`` built from the fetched creds), the credential
                 analog of a ``Provisioner``'s ``make_init_sql``. ``redact=True`` is for
                 ``--provision-dry-run``'s printed preview; None => this credential contributes no SQL
                 (a bare `--repl` on a credential-only suite stays a plain shell, as before).
                 CAVEAT: ``--repl`` reads back an ALREADY-fetched value (no re-fetch) -- if
                 ``available()`` short-circuited the up-front fetch (creds usable via preset env, never
                 written to the store), there's nothing to read back, so this contributes no SQL even
                 though the credential is genuinely usable. No current consumer combines ``available``
                 with ``to_init_sql``; if one needs both, ``to_init_sql`` should be prepared to build its
                 SQL from env directly rather than assume a store-backed value.
                 If the returned SQL needs an extension loaded (e.g. ``CREATE SECRET ... TYPE AZURE``),
                 prepend ``"require <ext>\\n\\n"`` rather than a ``LOAD <ext>;`` statement --
                 ``_write_init_sqllogic_snippet`` (``plugin.py``) pulls `require` lines out ahead of the
                 wrapping ``statement ok`` block (a directive can't go inside one) and routes them
                 through the SAME reliable extension-loading path a `.test` file's own `require` uses
                 (`SQLLogicTestRunner::LoadExtension`: static/linked first, then `INSTALL ... FROM` the
                 compile-time-known local repo, then `LOAD`). A bare `LOAD <ext>;` inside the SQL text
                 skips all of that and only checks `$HOME/.duckdb`'s cache -- it "works" only by
                 accident, wherever that happens to already be populated.
    """

    key: str
    fetch: Callable
    validate: Optional[Callable] = None
    error: Optional[Callable] = None
    adopt: Optional[str] = None
    available: Optional[Callable] = None
    late_fetch: bool = True
    to_init_sql: Optional[Callable] = None


@dataclass(frozen=True)
class Service:
    """A class-2 service descriptor — lazy, first-worker-wins, controller-torn-down.

    Fields (held only in Phase 0):
      key     : logical service name (the OSS docker container is the model).
      start   : ``start(config)`` — brings the service up (UC's ``start_container``).
                ``None`` => this service has NO managed lifecycle at all: it permanently exists
                outside this run's control (a real cloud account, a public read-only server) --
                RESOURCE-PLANNING.md's ``invocation-external`` (phase 5). ``provision_service``
                then routes straight to ``attach()`` unconditionally, whether or not
                ``--existing-service`` was declared -- there is nothing else it COULD do. No new
                descriptor type: this is the same ``Service`` shape, just without a boot.
      stop    : ``stop(config)`` — tears it down once on the controller (UC's
                ``teardown_shared``); None => no explicit teardown.
      fixture : name of the session fixture this service backs (e.g. ``"uc_server"``), used
                for gating in a later phase; the fixture body itself stays in the backend.
      attach  : ``attach(overrides, config) -> block`` — build this service's block from an
                override map when the service is declared **existing** (``--existing-service``),
                i.e. already running and NOT managed by this run. ``overrides`` is ``{}`` for the
                all-defaults form, ``{"endpoint": url}`` for the ``KEY=URL`` form, or a full dict
                for the ``KEY={json}`` form. None => the service can't be externalized (attaching
                falls back to using the raw overrides as the block). See ``docs/SERVICES.md``.
      alive   : ``alive(block) -> bool`` — a cheap, non-authenticating liveness probe. Run once on
                attach so a declared-but-dead service FAILS LOUD instead of dying opaquely in a query;
                None => no probe (attach trusts the declaration).
      depends_on: keys of other registered services that must be up first. RESERVED — the field +
                validation land now (so the ``Service`` shape is agreed across the three converging
                efforts), but start-order resolution + reverse-order teardown are implemented with the
                multi-service work (docs/PLAN.md § *Pre-0.1 release gates*, driven by Iceberg's
                ``rest``→``minio``). ``()`` => no dependencies.
      to_env  : ``to_env(block) -> dict`` — a derived env map merged into ``os.environ`` (the service
                analog of ``credential(adopt="env")``); how a test gets a service's connection env.
                Adopted by EVERY process that provisions the service (in ``provision_service``) — the
                ``.test`` subprocess inherits its provisioning process's ``os.environ`` (``_invoke``
                merges it). Since the service is provisioned up front from the collect-first plan,
                even a bare ``.test`` (no ``.py`` driver, no fixture to pull) gets its env. None => no
                env adoption. (WHAT env, not WHEN — orthogonal to the removed disposition.)
      populate: ``populate(block, config)`` — bring the service to its known initial state (structure +
                data — the store-scope analog of the fixture lane's ``instantiate``), run ONCE after the
                service is up. It mutates the shared service, not per-process state (HOW the service is
                seeded, not WHEN it is provisioned). MUST be idempotent (it re-runs against an attached,
                possibly-seeded instance). None => nothing to populate.
      to_init_sql: ``to_init_sql(block, *, redact=False) -> str`` — SQL fed into a ``--repl`` session's
                ``-init`` file (e.g. a ``CREATE SECRET`` built from the block's connection string), the
                service analog of a ``Provisioner``'s ``make_init_sql``. Without it, ``--repl`` on a
                service-backed, provisioner-less suite (a bare-``.test`` suite like azurite) drops you
                into a shell with the connection env set but no secret/``USE`` typed for you — this is
                what closes that gap. ``redact=True`` is for ``--provision-dry-run``'s printed preview
                (mask the secret material, keep the shape). None => this service contributes no SQL.
                Also feeds `auto_init_sql`/`--init-sqllogic` (a bare `.test` item), not just `--repl` --
                see :class:`Credential`'s `to_init_sql` docstring for the `"require <ext>\\n\\n"`
                prepend convention if the SQL needs an extension loaded.

    Policy fields (``to_env`` / ``populate`` / ``to_init_sql``) are usually set via :func:`use_service`,
    which binds a *shared* descriptor (e.g. ``AZURITE_SERVICE``) to one suite's policy without mutating
    the shared one. See ``docs/SERVICES.md``.
    """

    key: str
    start: Optional[Callable] = None
    stop: Optional[Callable] = None
    fixture: Optional[str] = None
    attach: Optional[Callable] = None
    alive: Optional[Callable] = None
    depends_on: Tuple[str, ...] = ()
    to_env: Optional[Callable] = None
    populate: Optional[Callable] = None
    to_init_sql: Optional[Callable] = None


@dataclass(frozen=True)
class Suite:
    """A named subset of the tests + its default-selection policy + up-front resources.

    Fields:
      name        : the suite's name; also the default marker (see ``marker``).
      path        : repo-relative dir whose members belong to the suite (path-based
                    membership); None => no path membership.
      marker      : the marker auto-applied to members (``-m <marker>`` selection);
                    defaults to ``name``.
      default     : whether the suite runs on a bare invocation (default-in polarity).
                    ``False`` => an opt-in heavy suite, deselected on a bare run in Phase 1.
      credentials : tuple of ``Credential`` descriptors (class-1).
      services    : tuple of ``Service`` descriptors (class-2).
      provisioner : the backend provisioner for this suite's ``@requires`` tests, or None.
      matrix      : tuple of cell dicts every member of this suite fans out across — the suite-level
                    counterpart of a per-test ``@requires_matrix`` (docs/RESOURCE-PLANNING.md §5 phase
                    9). Each cell is an OPEN, backend-interpreted dict, same spirit as a
                    ``Requirement.properties`` -- the framework validates only that ``"backend"`` is
                    present (it's the id a `.test` sibling's name and a `.py` cell's ``pytest.param``
                    id are built from); everything else is for the backend's own conftest/provisioner
                    to read (e.g. via ``matrix_cell``, same spirit as ``requires.py``'s
                    ``Requirement.properties``). A bare `.test`'s subprocess specifically reads a
                    ``"properties"`` key (``sqllogic.py``'s ``_matrix_cell_properties``): declare WHAT
                    a cell needs (``temp_dir_root``, ``data_dir``, or any plain env var name) and the
                    framework decides downstream whether that becomes a ``--temp-dir-base``/
                    ``--data-dir`` CLI arg or a literal env var (``_split_matrix_cell_properties``) --
                    never the conftest's call. ``()`` => no suite-level matrix (today's behavior,
                    unchanged).
      auto_init_sql: whether a bare `.test` item in this suite gets this suite's credentials'/
                    services' ``to_init_sql`` output run (via upstream's ``--init-sqllogic``) before
                    its body -- the non-``--repl`` generalization of ``to_init_sql`` (until now, a
                    resource's init SQL only ever reached an interactive ``--repl`` session; a bare
                    `.test` had to hand-write its own ``CREATE SECRET`` instead). ``False`` => today's
                    behavior, unchanged.
    """

    name: str
    path: Optional[str] = None
    marker: Optional[str] = None
    default: bool = True
    credentials: Tuple[Credential, ...] = ()
    services: Tuple[Service, ...] = ()
    provisioner: object = field(default=None)
    matrix: Tuple[dict, ...] = ()
    auto_init_sql: bool = False


def credential(
    key, *, fetch, validate=None, error=None, adopt=None, available=None, late_fetch=True, to_init_sql=None
) -> Credential:
    """Build a frozen :class:`Credential` descriptor (see its docstring for the fields).

    Validates only shape: ``key`` non-empty, ``fetch`` (and any ``validate`` / ``error`` /
    ``available`` / ``to_init_sql``) callable, ``adopt`` one of ``None`` / ``"env"``. Nothing is
    fetched — Phase 0 just holds the callables.
    """
    if not key or not isinstance(key, str):
        raise ValueError("credential: `key` must be a non-empty string")
    if not callable(fetch):
        raise TypeError("credential: `fetch` must be callable (fetch(config) -> value)")
    if validate is not None and not callable(validate):
        raise TypeError("credential: `validate` must be callable or None")
    if error is not None and not callable(error):
        raise TypeError("credential: `error` must be callable or None")
    if available is not None and not callable(available):
        raise TypeError("credential: `available` must be callable or None")
    if adopt not in (None, "env"):
        raise ValueError(f"credential: `adopt` must be None or 'env', got {adopt!r}")
    if to_init_sql is not None and not callable(to_init_sql):
        raise TypeError("credential: `to_init_sql` must be callable or None (to_init_sql(value, *, redact) -> str)")
    return Credential(
        key=key,
        fetch=fetch,
        validate=validate,
        error=error,
        adopt=adopt,
        available=available,
        late_fetch=bool(late_fetch),
        to_init_sql=to_init_sql,
    )


def service(
    key,
    *,
    start=None,
    stop=None,
    fixture=None,
    attach=None,
    alive=None,
    depends_on=(),
    to_env=None,
    populate=None,
    to_init_sql=None,
) -> Service:
    """Build a frozen :class:`Service` descriptor (see its docstring for the fields).

    Validates only shape: ``key`` non-empty, the callables callable, ``fixture`` a string or None,
    ``depends_on`` a tuple of keys. Nothing is started here. Policy (``to_env`` / ``populate`` /
    ``to_init_sql``) is usually applied via :func:`use_service`.

    ``start=None`` declares a service with NO managed lifecycle -- ``invocation-external``
    (RESOURCE-PLANNING.md phase 5): it just permanently exists, so ``provision_service`` always
    attaches (never boots), whether or not ``--existing-service`` was declared for it.
    """
    if not key or not isinstance(key, str):
        raise ValueError("service: `key` must be a non-empty string")
    if start is not None and not callable(start):
        raise TypeError("service: `start` must be callable or None (start(config); None = invocation-external)")
    if stop is not None and not callable(stop):
        raise TypeError("service: `stop` must be callable or None")
    if fixture is not None and not isinstance(fixture, str):
        raise TypeError("service: `fixture` must be a string or None")
    if attach is not None and not callable(attach):
        raise TypeError("service: `attach` must be callable or None (attach(overrides, config) -> block)")
    if alive is not None and not callable(alive):
        raise TypeError("service: `alive` must be callable or None (alive(block) -> bool)")
    if to_env is not None and not callable(to_env):
        raise TypeError("service: `to_env` must be callable or None (to_env(block) -> dict)")
    if populate is not None and not callable(populate):
        raise TypeError("service: `populate` must be callable or None (populate(block, config))")
    if to_init_sql is not None and not callable(to_init_sql):
        raise TypeError("service: `to_init_sql` must be callable or None (to_init_sql(block, *, redact) -> str)")
    deps = tuple(depends_on)
    if not all(isinstance(d, str) and d for d in deps):
        raise TypeError("service: `depends_on` must be a tuple of non-empty service-key strings")
    return Service(
        key=key,
        start=start,
        stop=stop,
        fixture=fixture,
        attach=attach,
        alive=alive,
        depends_on=deps,
        to_env=to_env,
        populate=populate,
        to_init_sql=to_init_sql,
    )


def use_service(base, *, to_env=None, populate=None, depends_on=None, to_init_sql=None) -> Service:
    """Bind a *shared* :class:`Service` descriptor to one suite's policy.

    Returns a COPY of ``base`` with the policy fields set — so a shared descriptor (e.g. azurite's
    ``AZURITE_SERVICE``, reused across azure/delta/uc) stays generic while each suite supplies its own
    ``to_env`` (derived env), ``populate`` (structure+data), and optionally overrides ``to_init_sql``
    (``--repl`` init SQL — most suites just inherit the shared descriptor's, e.g. azurite's default
    connection secret). Values not given fall back to ``base``'s. See ``docs/SERVICES.md``.
    """
    import dataclasses

    if not isinstance(base, Service):
        raise TypeError("use_service: `base` must be a service(...) descriptor")
    if to_env is not None and not callable(to_env):
        raise TypeError("use_service: `to_env` must be callable or None")
    if populate is not None and not callable(populate):
        raise TypeError("use_service: `populate` must be callable or None")
    if to_init_sql is not None and not callable(to_init_sql):
        raise TypeError("use_service: `to_init_sql` must be callable or None")
    deps = base.depends_on if depends_on is None else tuple(depends_on)
    if not all(isinstance(d, str) and d for d in deps):
        raise TypeError("use_service: `depends_on` must be a tuple of non-empty service-key strings")
    return dataclasses.replace(
        base,
        to_env=to_env if to_env is not None else base.to_env,
        to_init_sql=to_init_sql if to_init_sql is not None else base.to_init_sql,
        populate=populate if populate is not None else base.populate,
        depends_on=deps,
    )


def register_suite(
    config,
    name,
    *,
    path=None,
    marker=None,
    default=True,
    credentials=(),
    services=(),
    provisioner=None,
    matrix=(),
    auto_init_sql=False,
) -> None:
    """Register a suite on ``config`` (call from a ``test/conftest.py`` ``pytest_configure``).

    Stores a frozen :class:`Suite` in ``config._duckdb_suites`` (a list created lazily, the
    same idiom as ``register_broadcast`` / ``register_provisioner``). ``marker`` defaults
    to ``name``. ``credentials`` / ``services`` must be the descriptors built by
    :func:`credential` / :func:`service`. Phase 0 records only — no fetch, no selection,
    no hooks.

    ``matrix`` is the suite-level fan-out: a list of cell dicts, each requiring a ``"backend"``
    key (see :class:`Suite`). Fan-out itself (`.py` via ``pytest_generate_tests``, `.test` via a
    post-collection splice) is Phase-9 behavior, not performed here — this call only validates
    shape and holds the cells.

    ``auto_init_sql`` opts every bare `.test` item in this suite into its credentials'/services'
    ``to_init_sql`` output (see :class:`Suite`).

    Re-registering an already-registered ``name`` **raises** ``ValueError`` (a duplicate
    suite name is almost certainly a double-declaration bug; failing loud matches the
    suite design's "any change must be loud" stance). Retrieve with :func:`get_suites`.
    """
    if not name or not isinstance(name, str):
        raise ValueError("register_suite: `name` must be a non-empty string")
    creds = tuple(credentials)
    for c in creds:
        if not isinstance(c, Credential):
            raise TypeError(
                f"register_suite: `credentials` must be credential(...) descriptors, got {type(c).__name__}"
            )
    svcs = tuple(services)
    for s in svcs:
        if not isinstance(s, Service):
            raise TypeError(f"register_suite: `services` must be service(...) descriptors, got {type(s).__name__}")
    cells = tuple(matrix)
    for cell in cells:
        if not isinstance(cell, dict) or not cell.get("backend"):
            raise TypeError(
                f"register_suite: each `matrix` cell must be a dict with a non-empty 'backend' key, got {cell!r}"
            )
    if not isinstance(auto_init_sql, bool):
        raise TypeError("register_suite: `auto_init_sql` must be a bool")

    regs = getattr(config, _ATTR, None)
    if regs is None:
        regs = []
        setattr(config, _ATTR, regs)
    if any(t.name == name for t in regs):
        raise ValueError(f"register_suite: suite {name!r} is already registered")

    regs.append(
        Suite(
            name=name,
            path=path,
            marker=marker if marker is not None else name,
            default=default,
            credentials=creds,
            services=svcs,
            provisioner=provisioner,
            matrix=cells,
            auto_init_sql=auto_init_sql,
        )
    )


def get_suites(config) -> list:
    """Return the list of registered :class:`Suite`s on ``config`` (``[]`` if none)."""
    return list(getattr(config, _ATTR, None) or [])
