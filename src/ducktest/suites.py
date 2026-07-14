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
- **class-2 service** — lazy, first-worker-wins, torn down once by the controller.
  ``service(key, start=..., stop=..., fixture=...)``.

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
    """

    key: str
    fetch: Callable
    validate: Optional[Callable] = None
    error: Optional[Callable] = None
    adopt: Optional[str] = None
    available: Optional[Callable] = None
    late_fetch: bool = True


@dataclass(frozen=True)
class Service:
    """A class-2 service descriptor — lazy, first-worker-wins, controller-torn-down.

    Fields (held only in Phase 0):
      key     : logical service name (the OSS docker container is the model).
      start   : ``start(config)`` — brings the service up (UC's ``start_container``). The
                lone required callable.
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
    """

    key: str
    start: Callable
    stop: Optional[Callable] = None
    fixture: Optional[str] = None
    attach: Optional[Callable] = None
    alive: Optional[Callable] = None


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
    """

    name: str
    path: Optional[str] = None
    marker: Optional[str] = None
    default: bool = True
    credentials: Tuple[Credential, ...] = ()
    services: Tuple[Service, ...] = ()
    provisioner: object = field(default=None)


def credential(key, *, fetch, validate=None, error=None, adopt=None, available=None, late_fetch=True) -> Credential:
    """Build a frozen :class:`Credential` descriptor (see its docstring for the fields).

    Validates only shape: ``key`` non-empty, ``fetch`` (and any ``validate`` / ``error`` /
    ``available``) callable, ``adopt`` one of ``None`` / ``"env"``. Nothing is fetched — Phase 0
    just holds the callables.
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
    return Credential(
        key=key,
        fetch=fetch,
        validate=validate,
        error=error,
        adopt=adopt,
        available=available,
        late_fetch=bool(late_fetch),
    )


def service(key, *, start, stop=None, fixture=None, attach=None, alive=None) -> Service:
    """Build a frozen :class:`Service` descriptor (see its docstring for the fields).

    Validates only shape: ``key`` non-empty, ``start`` (and any ``stop`` / ``attach`` / ``alive``)
    callable, ``fixture`` a string or None. Nothing is started — Phase 0 just holds the callables.
    """
    if not key or not isinstance(key, str):
        raise ValueError("service: `key` must be a non-empty string")
    if not callable(start):
        raise TypeError("service: `start` must be callable (start(config))")
    if stop is not None and not callable(stop):
        raise TypeError("service: `stop` must be callable or None")
    if fixture is not None and not isinstance(fixture, str):
        raise TypeError("service: `fixture` must be a string or None")
    if attach is not None and not callable(attach):
        raise TypeError("service: `attach` must be callable or None (attach(overrides, config) -> block)")
    if alive is not None and not callable(alive):
        raise TypeError("service: `alive` must be callable or None (alive(block) -> bool)")
    return Service(key=key, start=start, stop=stop, fixture=fixture, attach=attach, alive=alive)


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
) -> None:
    """Register a suite on ``config`` (call from a ``test/conftest.py`` ``pytest_configure``).

    Stores a frozen :class:`Suite` in ``config._duckdb_suites`` (a list created lazily, the
    same idiom as ``register_broadcast`` / ``register_provisioner``). ``marker`` defaults
    to ``name``. ``credentials`` / ``services`` must be the descriptors built by
    :func:`credential` / :func:`service`. Phase 0 records only — no fetch, no selection,
    no hooks.

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
        )
    )


def get_suites(config) -> list:
    """Return the list of registered :class:`Suite`s on ``config`` (``[]`` if none)."""
    return list(getattr(config, _ATTR, None) or [])
