"""Self-tests for the canonical per-item `Key` (`ducktest.decorate`, RESOURCE-PLANNING.md phases
1 + 4).

Pure, offline: plain fakes stand in for a `SqlLogicItem` (mirrors test_suites.py's `_Cfg` idiom) --
no pytest run needed, this is a pure data/function unit.
"""

from ducktest.collect import _batch_key
from ducktest.decorate import Key, coordination_key, decorate, rw_identity


class _FakeItem:
    """Minimal stand-in carrying only what `decorate`/`_batch_key` read."""

    def __init__(self, binary=None, working_dir=None, cell=None):
        if binary is not None:
            self._binary = binary
        if working_dir is not None:
            self._working_dir = working_dir
        if cell is not None:
            self._cell = cell


def test_decorate_returns_none_for_non_sqllogic_item():
    assert decorate(_FakeItem()) is None
    assert decorate(_FakeItem(binary="/bin/unittest")) is None  # missing working_dir
    assert decorate(_FakeItem(working_dir="/work")) is None  # missing binary
    assert decorate(object()) is None


def test_decorate_carries_build_and_run_setting():
    item = _FakeItem(binary="/bin/unittest", working_dir="/work")
    key = decorate(item)
    assert key == Key(build="/bin/unittest", run_setting="/work")


def test_batch_key_is_a_projection_of_the_canonical_key():
    item = _FakeItem(binary="/bin/unittest", working_dir="/work")
    key = decorate(item)
    # 4th component: the resolved --init-sqllogic argv, () for a fake with no real `.config`.
    assert _batch_key(item) == (key.build, key.run_setting, key.cell, ())
    assert _batch_key(_FakeItem()) is None


def test_batch_key_matches_on_equal_build_and_working_dir_only():
    a = _FakeItem(binary="/bin/unittest", working_dir="/work")
    b = _FakeItem(binary="/bin/unittest", working_dir="/work")
    c = _FakeItem(binary="/bin/other", working_dir="/work")
    assert _batch_key(a) == _batch_key(b)
    assert _batch_key(a) != _batch_key(c)


# --- phase 4: backend/access/cell + the named sharing guarantees -------------------------------


def test_decorate_defaults_backend_access_cell_to_none():
    item = _FakeItem(binary="/bin/unittest", working_dir="/work")
    key = decorate(item)
    assert (key.backend, key.access, key.cell) == (None, None, None)


def test_decorate_accepts_backend_access_cell_overrides():
    item = _FakeItem(binary="/bin/unittest", working_dir="/work")
    key = decorate(item, backend="azurite-az", access="ro", cell="managed")
    assert key == Key(build="/bin/unittest", run_setting="/work", backend="azurite-az", access="ro", cell="managed")
    # _batch_key reads `_cell` off the item itself, not decorate()'s override args above
    assert _batch_key(item) == (key.build, key.run_setting, None, ())


def test_batch_key_is_cell_aware():
    # a suite-matrix `.test` sibling stamps `_cell` (plugin.py); _batch_key must fold it in so two
    # cells of the SAME file never share a batch key (Catch2 can't attribute 2 outcomes to 1 test),
    # while different files in the SAME cell still match (still share a batch).
    a1 = _FakeItem(binary="/bin/unittest", working_dir="/work", cell="azurite-az")
    a2 = _FakeItem(binary="/bin/unittest", working_dir="/work", cell="azurite-az")
    b = _FakeItem(binary="/bin/unittest", working_dir="/work", cell="azure-az")
    assert _batch_key(a1) == _batch_key(a2)
    assert _batch_key(a1) != _batch_key(b)


def test_coordination_key_is_none_for_rw_or_unresolved_backend():
    assert coordination_key(None) is None
    assert coordination_key(Key(build="b", access="rw", backend="azurite-az")) is None  # rw is always private
    assert coordination_key(Key(build="b", access="ro", backend=None)) is None  # unresolved backend


def test_coordination_key_matches_iff_backend_and_access_match():
    ro_a1 = Key(build="b1", run_setting="w1", access="ro", backend="azurite-az")
    ro_a2 = Key(build="b2", run_setting="w2", access="ro", backend="azurite-az")  # build/run_setting differ
    ro_b = Key(build="b1", access="ro", backend="uc-databricks")
    assert coordination_key(ro_a1) == coordination_key(ro_a2) == ("azurite-az", "ro")
    assert coordination_key(ro_a1) != coordination_key(ro_b)


def test_rw_identity_is_none_for_non_rw():
    assert rw_identity(None, "n1") is None
    assert rw_identity(Key(build="b", access="ro", backend="x"), "n1") is None
    assert rw_identity(Key(build="b", access=None), "n1") is None


def test_rw_identity_unique_per_nodeid_stable_per_build_and_backend():
    key = Key(build="b1", access="rw", backend="azure-az")
    assert rw_identity(key, "test_a.py::test_1") != rw_identity(key, "test_a.py::test_2")
    assert rw_identity(key, "test_a.py::test_1") == rw_identity(key, "test_a.py::test_1")  # stable, repeatable
