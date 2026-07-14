"""Generic `@requires` marker: declares a test's external resource needs.

GENERIC and extension-AGNOSTIC. This is a thin pytest marker that records a
small, flat spec per requirement; it does NOT interpret the spec. The concrete
meaning of the `properties` dict (and how to actually provision) is the extension
provisioner's job (see the extension's `test/py/<repo>/` package). Keeping the
marker dumb is deliberate: the framework stays portable across extensions.

Usage (in a driver `.py`):

    from ducktest import requires

    @requires(source="${CATALOG}.source.simple_table",
              access="rw", properties={"commit": "cmt", "storage": "managed"})
    def test_write_catalog_managed(request):
        ...

The decorator STACKS — apply it multiple times for multiple resources:

    @requires(source="${CAT}.source.a", access="ro")
    @requires(source="${CAT}.source.b", access="rw", properties={"storage": "external"})
    def test_two_tables(request):
        ...

Each application appends one `Requirement` to the test's `requires` marker. Read
them back (in a hook or the provisioner) via `collect_requirements(item)`.

`@requires_matrix` fans one body out across a CI-style matrix of cells (see its
docstring): each cell is an independent pytest item carrying its OWN per-cell
`@requires`, so `resources`/provisioning work unchanged per cell.
"""

import itertools
from dataclasses import dataclass, field

import pytest

from .fixtures import Fixture

# Marker name carrying the stacked requirements.
MARKER = "requires"


@dataclass(frozen=True)
class Requirement:
    """One declared resource need. Flat by design (see driver/README.md "Resources").

    Fields:
      source     : where the table comes from. One of:
                     - a ``Fixture("name")`` ref (SQL definition + seed, instantiated via
                       duckdb — see fixtures.py);
                     - a premade source table FQN string, env-templated (``${VAR}`` expanded
                       at provision time, NOT here) — e.g. ``${CATALOG}.source.simple_table``;
                     - a backend-defined lazy ref (any other object — e.g. an extension's own
                       ``IcebergDef``) — OPAQUE to the framework, interpreted entirely by the
                       provisioner's ``instantiate()`` hook, the same way ``properties`` is
                       open/backend-interpreted. ``name=`` is REQUIRED in this case (see
                       ``resolved_name()`` — there's no generic way to derive a bare name from
                       an arbitrary object).
      access     : ``ro`` (shared, reference source directly) | ``rw`` (exclusive, the
                   provisioner clones into an isolated namespace). A framework knob
                   (sharing/isolation), so it stays a top-level field.
      properties : an OPEN, backend-interpreted dict — e.g. ``{"commit": "cmt",
                   "storage": "managed"}``. The generic driver does NOT interpret or
                   validate these; the extension provisioner reads them (via
                   ``prop.property(key)``) and decides what they imply. Empty by default.
      name       : the table's BARE name in the provisioned schema. Defaults to the
                   source's base table name (last dotted segment of ``source``) for a
                   ``Fixture``/string source; REQUIRED for a backend-defined lazy ref.

    A dict field would make instances unhashable IF hashed; they aren't (stored as mark
    args, iterated not hashed), so ``frozen=True`` + a mutable ``properties`` is safe.
    """

    source: str
    access: str = "ro"
    properties: dict = field(default_factory=dict)
    name: str = None

    def property(self, key, default=None):
        """Read one backend-interpreted property (``None`` if absent)."""
        return self.properties.get(key, default)

    def resolved_name(self) -> str:
        """Bare table name to use in the provisioned schema (default: source's base).

        A backend-defined lazy-ref source (neither a ``Fixture`` nor a string) has no generic
        way to derive a bare name, so it always falls in the ``self.name`` branch above —
        ``requires()`` enforces ``name=`` is given for that case, so this never reaches the
        string-splitting fallback for one.
        """
        if self.name:
            return self.name
        if isinstance(self.source, Fixture):
            # A fixture's bare name is its logical name (the CREATE'd table matches it).
            return self.source.name
        # source may still contain ${...}; take the literal last segment. Env
        # expansion happens in the provisioner, but the base table name is the
        # last dotted token regardless of expansion.
        return self.source.rsplit(".", 1)[-1]


def requires(source, access="ro", properties=None, name=None):
    """Stackable marker declaring one resource requirement. See module docstring.

    Validates only what the framework owns (``source``, ``access``); the ``properties``
    dict is passed through opaque — backends validate their own keys. ``${...}`` is NOT
    expanded here (the provisioner's job).

    ``source`` is a ``Fixture(...)`` ref, a table FQN string, or a backend-defined lazy ref
    (any other truthy object — e.g. an extension's own ``IcebergDef``; interpreted entirely by
    the provisioner's ``instantiate()``, the same open/backend-interpreted spirit as
    ``properties``). That third case REQUIRES an explicit ``name=`` — ``resolved_name()`` has
    no generic way to derive a bare name from an arbitrary object.
    """
    if isinstance(source, Fixture):
        pass  # a named fixture ref — instantiated by the backend instantiator (fixtures.py)
    elif isinstance(source, str):
        if not source:
            raise ValueError("@requires: `source` must be a Fixture(...) ref, a non-empty table FQN string, or a backend-defined lazy ref (with `name=`)")
    elif not source:
        raise ValueError("@requires: `source` must be a Fixture(...) ref, a table FQN string, or a backend-defined lazy ref (with `name=`)")
    elif not name:
        raise ValueError(
            f"@requires: source={source!r} is neither a Fixture(...) nor a string, so it's a "
            "backend-defined lazy ref (opaque to the framework) -- `name=` is required for one, "
            "since resolved_name() can't derive a bare name from an arbitrary object."
        )
    if access not in ("ro", "rw"):
        raise ValueError(f"@requires: access must be 'ro' or 'rw', got {access!r}")

    req = Requirement(source=source, access=access, properties=dict(properties or {}), name=name)
    # pytest.mark.requires(req); stacking yields one mark per application, each with
    # its own args — collect_requirements() flattens them back in declaration order.
    return getattr(pytest.mark, MARKER)(req)


def collect_requirements(item) -> list:
    """Return the list of `Requirement`s declared on a pytest item (in source order).

    Empty list if the item carries no `@requires`. pytest yields stacked marks
    nearest-decorator-first; we reverse so the result matches top-to-bottom source
    order, which is the order a reader expects. Per-cell `requires` marks emitted by
    `@requires_matrix` (via `pytest.param(marks=...)`) are included natively.
    """
    reqs = []
    for mark in item.iter_markers(name=MARKER):
        # each mark from one @requires application: args == (Requirement,)
        if mark.args and isinstance(mark.args[0], Requirement):
            reqs.append(mark.args[0])
    reqs.reverse()
    return reqs


# ---------------------------------------------------------------------------
# @requires_matrix — fan one body out across a CI-style matrix of cells
# ---------------------------------------------------------------------------


def expand_cells(properties) -> list:
    """Expand a `properties` spec into the list of concrete per-cell property dicts.

    PURE (no pytest, no I/O): the matrix's generation stage, separate from emission.
    Any value that is a **list is an axis**; scalars are fixed across every cell. The
    result is the cartesian product of the axes, each with the fixed keys merged in.
    Zero axes (all scalars) => a single cell (`itertools.product()` yields one empty
    tuple). Axis-key order is preserved (dict insertion order) so cell ids are stable.

    This list-of-dicts is the SEAM: a future capability-table hole-filter, or an
    explicit/combined cell set, replaces or post-filters this list without touching the
    emission below.
    """
    properties = dict(properties or {})
    axis_keys = [k for k, v in properties.items() if isinstance(v, list)]
    fixed = {k: v for k, v in properties.items() if not isinstance(v, list)}
    cells = []
    for combo in itertools.product(*(properties[k] for k in axis_keys)):
        cell = dict(fixed)
        cell.update(zip(axis_keys, combo))
        cells.append((axis_keys, cell))
    return cells


def requires_matrix(source, access="ro", properties=None, name=None, marks=()):
    """Fan one test body out across a matrix of cells; each cell is its own pytest item.

    A `@requires_matrix` IS a `@requires` that varies per cell: it emits one
    `pytest.param` per cell, each carrying its own per-cell `requires(...)` mark (read
    back by `collect_requirements`) plus any user `marks` (for `-m` selection). A static
    `@requires` can't vary per parametrize cell; this can.

        @requires_matrix(source=Fixture("id_name").Seed(None), access="rw",
                         properties={"storage": ["managed", "external"]},  # list => axis
                         marks=["oss_local"])                              # cell tags
        def test_rw(request, resources): ...
        # -> items test_rw[managed], test_rw[external]

    Cell generation (`expand_cells`) is separate from emission, so explicit/combined cell
    sets and per-backend hole-filtering can slot in at the list-of-dicts seam later.

    `marks` entries may be marker names (str -> `pytest.mark.<name>`) or MarkDecorators.
    Emits an `indirect=True` parametrize over `matrix_cell` — the value routes through the
    `matrix_cell` fixture (plugin.py) since the body has no `matrix_cell` argument.
    """
    user_marks = [getattr(pytest.mark, m) if isinstance(m, str) else m for m in marks]
    params = []
    for axis_keys, cell in expand_cells(properties):
        cell_id = "-".join(str(cell[k]) for k in axis_keys) if axis_keys else None
        req_mark = requires(source, access=access, properties=cell, name=name)
        params.append(pytest.param(cell, id=cell_id, marks=[req_mark, *user_marks]))
    return pytest.mark.parametrize("matrix_cell", params, indirect=True)
