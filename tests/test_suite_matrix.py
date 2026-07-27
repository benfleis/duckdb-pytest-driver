"""Self-tests for suite-level matrix fan-out (RESOURCE-PLANNING.md §5 phase 9): a suite's
``matrix=`` elevates ``@requires_matrix``'s per-test cell fan-out to "every member of this suite
runs across these cells", for BOTH ``.py`` (``pytest_generate_tests``) and ``.test`` (a
post-collection splice in ``pytest_collection_modifyitems``). Offline throughout: real inner pytest
runs via ``pytester`` (subprocess, so hook registration + ordering is exercised for real), plus a
stub unittest binary (mirrors ``test_collector.py``) for the ``.test`` side — no duckdb build, no
docker, no network.
"""

import stat
import textwrap

from ducktest.collect import _batch_key
from ducktest.decorate import decorate


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# --- .py fan-out ---------------------------------------------------------------------------

_MATRIX_SUITE_CONFTEST = """
    from ducktest import register_suite

    def pytest_configure(config):
        register_suite(config, "matrixed", path="test/matrixed", matrix=[
            {"backend": "azurite-az"},
            {"backend": "azure-az"},
        ])
"""

_PLAIN_TEST = """
    def test_plain(matrix_cell):
        assert matrix_cell["backend"] in ("azurite-az", "azure-az")
"""

_OWN_MATRIX_TEST = """
    from ducktest import requires_matrix, TableSpec

    @requires_matrix(source=TableSpec("id_name").Seed(None), access="rw",
                     properties={"storage": ["x", "y"]})
    def test_explicit(request, matrix_cell):
        pass
"""


def _run(pytester, *args):
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)


def test_suite_matrix_fans_out_a_plain_py_test(pytester):
    _write(pytester, "test/conftest.py", _MATRIX_SUITE_CONFTEST)
    _write(pytester, "test/matrixed/test_plain.py", _PLAIN_TEST)
    result = _run(pytester, "-v", "test/matrixed")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*test_plain*azurite-az*", "*test_plain*azure-az*"])


def test_suite_matrix_ignores_a_test_with_no_matrix_cell_fixture(pytester):
    # a bare test that never touches matrix_cell/resources is untouched -- no accidental fan-out.
    _write(pytester, "test/conftest.py", _MATRIX_SUITE_CONFTEST)
    _write(pytester, "test/matrixed/test_bare.py", "def test_bare(): pass")
    result = _run(pytester, "test/matrixed")
    result.assert_outcomes(passed=1)


def test_own_requires_matrix_wins_over_suite_matrix(pytester):
    # explicit @requires_matrix wins outright: 2 items (its own storage axis), NOT 4 (2 suite cells
    # x 2 requires_matrix cells) -- see the plan's Composition question.
    _write(pytester, "test/conftest.py", _MATRIX_SUITE_CONFTEST)
    _write(pytester, "test/matrixed/test_explicit.py", _OWN_MATRIX_TEST)
    result = _run(pytester, "--collect-only", "-q", "test/matrixed")
    lines = [ln for ln in result.stdout.lines if "test_explicit[" in ln]
    assert len(lines) == 2
    result.stdout.fnmatch_lines(["*test_explicit*x*", "*test_explicit*y*"])


# --- .test fan-out ---------------------------------------------------------------------------

# Same stub-binary technique as test_collector.py: mimics --emit-test-events for whatever names
# it's asked to run, so no real duckdb binary is needed to prove the driver-side mechanism.
_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys

    def names(argv):
        out = []
        it = iter(argv)
        for a in it:
            if a == "-f":
                path = next(it, None)
                if path:
                    with open(path) as f:
                        out += [ln.strip() for ln in f if ln.strip()]
            elif a.endswith(".test"):
                out.append(a)
        return out

    for n in names(sys.argv[1:]):
        ev = {"event": "end", "name": n, "status": "ok", "passes": 1, "fails": 0, "skip-mode": 0}
        sys.stderr.write("[TEST_EVENT] " + json.dumps(ev) + "\\n")
    sys.exit(0)
    """
)


def _stub_binary(pytester):
    p = pytester.path / "stub_unittest"
    p.write_text(_STUB)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_dot_test_matrix_fans_out_one_file_into_n_cell_siblings(pytester):
    _write(pytester, "test/conftest.py", _MATRIX_SUITE_CONFTEST)
    _write(pytester, "test/matrixed/answer.test", "query I\nSELECT 42;\n----\n42\n")
    stub = _stub_binary(pytester)
    result = _run(pytester, "-v", "--unittest-binary", str(stub), "test/matrixed")
    result.assert_outcomes(passed=2)  # one per cell, same underlying file
    result.stdout.fnmatch_lines(["*answer.test*azurite-az*", "*answer.test*azure-az*"])


def test_dot_test_matrix_leaves_non_matrix_suites_untouched(pytester):
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite

        def pytest_configure(config):
            register_suite(config, "plain", path="test/plain")
        """,
    )
    _write(pytester, "test/plain/answer.test", "query I\nSELECT 42;\n----\n42\n")
    stub = _stub_binary(pytester)
    result = _run(pytester, "--unittest-binary", str(stub), "test/plain")
    result.assert_outcomes(passed=1)  # unchanged: no cell fan-out for a suite with no matrix=


# --- batching: cell is a batch-affinity component (collect.py) -------------------------------


def test_cell_fanned_siblings_of_the_same_file_never_share_a_batch_key():
    class _FakeItem:
        def __init__(self, binary, working_dir, cell):
            self._binary = binary
            self._working_dir = working_dir
            self._cell = cell

    same_file_cell_a = _FakeItem("/bin/unittest", "/work", "azurite-az")
    same_file_cell_b = _FakeItem("/bin/unittest", "/work", "azure-az")
    other_file_cell_a = _FakeItem("/bin/unittest", "/work", "azurite-az")

    # two cells of the SAME file -> different batch keys (never share one subprocess invocation).
    assert _batch_key(same_file_cell_a) != _batch_key(same_file_cell_b)
    # a DIFFERENT file in the SAME cell -> same batch key (still batches together).
    assert _batch_key(same_file_cell_a) == _batch_key(other_file_cell_a)
    # sanity: both are real projections of decorate()'s Key
    assert _batch_key(same_file_cell_a) == (
        decorate(same_file_cell_a, cell="azurite-az").build,
        decorate(same_file_cell_a, cell="azurite-az").run_setting,
        "azurite-az",
        (),  # no real `.config` on this fake -> no --init-sqllogic distinction
    )
