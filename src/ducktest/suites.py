"""Suite, credential, and service descriptors and registration."""

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from .context import get_context
from .provision import register_provisioner

# Config attribute holding the ordered list of registered Suites (mirrors
# `_duckdb_broadcast_factories` / `_driver_provisioners`). Created lazily.
_ATTR = "_duckdb_suites"


@dataclass(frozen=True)
class EnvRef:
    """A late-bound reference to another subprocess environment variable."""

    name: str


def env_ref(name: str) -> EnvRef:
    """Reference an environment value that will exist only when a test invocation is prepared."""
    if not isinstance(name, str) or not name:
        raise ValueError("env_ref: `name` must be a non-empty string")
    return EnvRef(name)


@dataclass(frozen=True)
class Credential:
    """Invocation-scoped credential policy.

    ``fetch(config)`` returns the credential value. ``available()`` can avoid fetching when ambient
    credentials are already usable. ``validate(value)`` and ``error()`` control failure reporting;
    ``adopt="env"`` publishes a mapping to the process environment and SQLLogic substitution.

    ``to_init_sql(value, *, redact=False)`` supplies REPL and optional bare-test initialization SQL.
    If ``available()`` short-circuits before a value is stored, initialization SQL is omitted. Prefix
    SQL that needs an extension with ``require <extension>``.
    """

    key: str
    fetch: Callable
    validate: Optional[Callable] = None
    error: Optional[Callable] = None
    adopt: Optional[str] = None
    available: Optional[Callable] = None
    to_init_sql: Optional[Callable] = None


@dataclass(frozen=True)
class Service:
    """Invocation-scoped managed or attached service.

    ``start(config)`` returns a serializable service block; ``stop(config)`` tears down a managed
    instance. With no ``start``, the service is external and uses ``attach(overrides, config)``.
    Managed and attached paths must return the same block shape. ``alive(block)`` validates attached
    instances. ``depends_on`` controls startup order.

    ``populate(block, config)`` initializes shared state and must be idempotent because it also runs for
    attached services. ``to_env(block)`` publishes values to the process environment and SQLLogic
    substitution. ``to_init_sql(block, *, redact=False)`` supplies REPL and optional bare-test SQL.
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
    """Named test set with selection and resource policy.

    ``path`` selects a subtree relative to the driver working directory; ``marker`` adds marker-based
    membership and defaults to ``name``. ``default=False`` excludes the suite from a bare run.

    A suite provisioner is scoped to ``path``. Each matrix cell requires an ``id``. Its ``properties``
    configure temp/data roots, test configuration, init SQL, and subprocess environment; see
    ``docs/MATRIX.md``. ``auto_init_sql`` applies credential and service initialization SQL to bare tests.
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


def credential(key, *, fetch, validate=None, error=None, adopt=None, available=None, to_init_sql=None) -> Credential:
    """Build a validated credential descriptor without fetching it."""
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
    """Build a validated service descriptor without starting it."""
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
    """Return a copy of ``base`` with suite-specific policy overrides."""
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
    """Register a suite during ``pytest_configure``.

    Names are unique. A provisioner requires ``path`` and is registered at that path relative to the
    driver working directory. Explicit provisioners registered below that path take precedence.

    Matrix cells must be dictionaries with a non-empty ``id``; an optional ``marks`` (list/tuple of
    marker names) is stamped on every item that cell fans out to, so ``-m`` can select a cell across
    every suite that has one, not just by ``id`` substring. Registration validates descriptors but
    does not fetch credentials, start services, or provision test resources.
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
        if not isinstance(cell, dict) or not cell.get("id"):
            raise TypeError(
                f"register_suite: each `matrix` cell must be a dict with a non-empty 'id' key, got {cell!r}"
            )
        marks = cell.get("marks", ())
        if not isinstance(marks, (list, tuple)) or not all(isinstance(m, str) and m for m in marks):
            raise TypeError(
                f"register_suite: matrix cell {cell['id']!r} 'marks' must be a list/tuple of non-empty "
                f"strings, got {marks!r}"
            )
    if not isinstance(auto_init_sql, bool):
        raise TypeError("register_suite: `auto_init_sql` must be a bool")
    if provisioner is not None and not path:
        raise ValueError("register_suite: `provisioner` requires a suite `path` for scoped registration")
    provisioner_scope = None
    if provisioner is not None:
        provisioner_scope = os.path.join(get_context(config).working_dir, path)
    regs = getattr(config, _ATTR, None)
    if regs is None:
        regs = []
        setattr(config, _ATTR, regs)
    if any(t.name == name for t in regs):
        raise ValueError(f"register_suite: suite {name!r} is already registered")

    suite = Suite(
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
    regs.append(suite)

    if provisioner is not None:
        register_provisioner(config, provisioner, scope=provisioner_scope)


def get_suites(config) -> list:
    """Return registered suites in declaration order."""
    return list(getattr(config, _ATTR, None) or [])
