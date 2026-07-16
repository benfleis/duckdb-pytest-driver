"""Self-tests for `@requires_matrix` (offline; pytester in-process, no duckdb build).

Proven without a provisioner: the body reads `request.node`/`matrix_cell` directly and
never requests `resources`, so no fixture is loaded and no container is touched. What we
assert is the MECHANISM — one item per cell, each with its own per-cell `requires` props
(via `collect_requirements`), the right ids, and the user `marks` applied.
"""

# The matrix test under test: a 2-value `storage` axis, tagged `oss_local`. `matrix_cell`
# is taken directly (indirect parametrize needs it in the closure; real drivers reach it
# via `resources`). TableSpec("id_name") stays a pure value — never resolved (no `resources`).
_MATRIX_TEST = """
from ducktest import requires_matrix, TableSpec, collect_requirements

@requires_matrix(source=TableSpec("id_name").Seed(None), access="rw",
                 properties={"storage": ["managed", "external"]},
                 marks=["oss_local"])
def test_rw(request, matrix_cell):
    reqs = collect_requirements(request.node)
    assert len(reqs) == 1                                   # one per-cell requirement
    assert reqs[0].access == "rw"                           # scalar carried through
    assert reqs[0].property("storage") == matrix_cell["storage"]  # per-cell axis value
    assert request.node.get_closest_marker("oss_local") is not None  # user mark applied
"""


def test_matrix_expands_per_cell_requirements(pytester):
    """Two cells -> two passing items, each with its own props + the `oss_local` mark."""
    pytester.makepyfile(_MATRIX_TEST)
    result = pytester.runpytest()
    result.assert_outcomes(passed=2)


def test_matrix_cell_ids(pytester):
    """Cell ids are the axis values only, so node ids read `test_rw[managed|external]`."""
    pytester.makepyfile(_MATRIX_TEST)
    result = pytester.runpytest("--collect-only", "-q")
    result.stdout.fnmatch_lines(["*test_rw*managed*", "*test_rw*external*"])


def test_expand_cells_is_pure_product():
    """The generation stage is separable + pure: axes -> cartesian product, scalars fixed."""
    from ducktest.requires import expand_cells

    cells = [cell for _keys, cell in expand_cells({"storage": ["managed", "external"], "commit": "cmt"})]
    assert cells == [
        {"commit": "cmt", "storage": "managed"},
        {"commit": "cmt", "storage": "external"},
    ]
    # Zero axes (all scalars) -> a single cell, so behavior is uniform.
    assert [c for _k, c in expand_cells({"storage": "managed"})] == [{"storage": "managed"}]
    assert [c for _k, c in expand_cells(None)] == [{}]
