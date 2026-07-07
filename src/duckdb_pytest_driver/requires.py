"""Generic `@requires` marker: declares a test's external resource needs.

GENERIC and extension-AGNOSTIC. This is a thin pytest marker that records a
small, flat spec per requirement; it does NOT interpret the spec. The concrete
meaning of `commit`/`storage` (and how to actually provision) is the extension
provisioner's job (see the extension's `test/py/<repo>/` package). Keeping the
marker dumb is deliberate: the framework stays portable across extensions.

Usage (in a driver `.py`):

    from driver import requires

    @requires(source="${CATALOG}.source.simple_table",
              access="rw", commit="cmt", storage="managed")
    def test_write_catalog_managed(request):
        ...

The decorator STACKS — apply it multiple times for multiple resources:

    @requires(source="${CAT}.source.a", access="ro")
    @requires(source="${CAT}.source.b", access="rw", storage="external")
    def test_two_tables(request):
        ...

Each application appends one `Requirement` to the test's `requires` marker. Read
them back (in a hook or the provisioner) via `collect_requirements(item)`.
"""

from dataclasses import dataclass

import pytest

from .fixtures import Fixture

# Marker name carrying the stacked requirements.
MARKER = "requires"


@dataclass(frozen=True)
class Requirement:
    """One declared resource need. Flat by design (see driver/README.md "Resources").

    Fields:
      source  : where the table comes from. Either a ``Fixture("name")`` ref
                (SQL definition + seed, instantiated via duckdb — see fixtures.py) or a
                premade source table FQN string, env-templated (``${VAR}`` expanded
                at provision time, NOT here) — e.g. ``${CATALOG}.source.simple_table``.
      access  : ``ro`` (shared, reference source directly) | ``rw`` (exclusive, the
                provisioner clones into an isolated namespace).
      commit  : ``cmt`` (catalog-managed commit protocol) | ``plain``. Uninterpreted
                here — the extension provisioner decides what props this implies.
      storage : ``managed`` (UC-managed, no LOCATION) | ``external`` (explicit
                LOCATION). Orthogonal to ``commit`` (the 2x2 cell).
      name    : the table's BARE name in the provisioned schema. Defaults to the
                source's base table name (last dotted segment of ``source``).
    """

    source: str
    access: str = "ro"
    commit: str = "cmt"
    storage: str = "managed"
    name: str = None

    def resolved_name(self) -> str:
        """Bare table name to use in the provisioned schema (default: source's base)."""
        if self.name:
            return self.name
        if isinstance(self.source, Fixture):
            # A fixture's bare name is its logical name (the CREATE'd table matches it).
            return self.source.name
        # source may still contain ${...}; take the literal last segment. Env
        # expansion happens in the provisioner, but the base table name is the
        # last dotted token regardless of expansion.
        return self.source.rsplit(".", 1)[-1]


def requires(source, access="ro", commit="cmt", storage="managed", name=None):
    """Stackable marker declaring one resource requirement. See module docstring.

    Validates the small enums up front (fail fast at decoration time) but does NOT
    expand ``${...}`` or interpret commit/storage — that is the provisioner's job.
    """
    if isinstance(source, Fixture):
        pass  # a named fixture ref — instantiated by the backend instantiator (fixtures.py)
    elif not source or not isinstance(source, str):
        raise ValueError("@requires: `source` must be a Fixture(...) ref or a table FQN string")
    if access not in ("ro", "rw"):
        raise ValueError(f"@requires: access must be 'ro' or 'rw', got {access!r}")
    if commit not in ("cmt", "plain"):
        raise ValueError(f"@requires: commit must be 'cmt' or 'plain', got {commit!r}")
    if storage not in ("managed", "external"):
        raise ValueError(f"@requires: storage must be 'managed' or 'external', got {storage!r}")

    req = Requirement(source=source, access=access, commit=commit, storage=storage, name=name)
    # pytest.mark.requires(req); stacking yields one mark per application, each with
    # its own args — collect_requirements() flattens them back in declaration order.
    return getattr(pytest.mark, MARKER)(req)


def collect_requirements(item) -> list:
    """Return the list of `Requirement`s declared on a pytest item (in source order).

    Empty list if the item carries no `@requires`. pytest yields stacked marks
    nearest-decorator-first; we reverse so the result matches top-to-bottom source
    order, which is the order a reader expects.
    """
    reqs = []
    for mark in item.iter_markers(name=MARKER):
        # each mark from one @requires application: args == (Requirement,)
        if mark.args and isinstance(mark.args[0], Requirement):
            reqs.append(mark.args[0])
    reqs.reverse()
    return reqs
