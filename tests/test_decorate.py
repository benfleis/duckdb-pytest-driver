"""Self-tests for the canonical per-item `Key` (`ducktest.decorate`, RESOURCE-PLANNING.md phase 1).

Pure, offline: plain fakes stand in for a `SqlLogicItem` (mirrors test_suites.py's `_Cfg` idiom) --
no pytest run needed, this is a pure data/function unit.
"""

from ducktest.collect import _batch_key
from ducktest.decorate import Key, decorate


class _FakeItem:
    """Minimal stand-in carrying only what `decorate` reads."""

    def __init__(self, binary=None, working_dir=None):
        if binary is not None:
            self._binary = binary
        if working_dir is not None:
            self._working_dir = working_dir


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
    assert _batch_key(item) == (key.build, key.run_setting)
    assert _batch_key(_FakeItem()) is None


def test_batch_key_matches_on_equal_build_and_working_dir_only():
    a = _FakeItem(binary="/bin/unittest", working_dir="/work")
    b = _FakeItem(binary="/bin/unittest", working_dir="/work")
    c = _FakeItem(binary="/bin/other", working_dir="/work")
    assert _batch_key(a) == _batch_key(b)
    assert _batch_key(a) != _batch_key(c)
