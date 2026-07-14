"""Self-tests for @requires' source-kind validation (requires.py).

`source` accepts a Fixture(...) ref, a table FQN string, or a backend-defined lazy ref (any
other object, opaque to the framework -- e.g. an extension's own IcebergDef). These exercise
the validation directly (no pytest collection needed).
"""

import pytest

from ducktest import Fixture, Requirement, requires


class _LazyRefStub:
    """Stand-in for a backend-defined lazy ref (e.g. an extension's IcebergDef)."""

    def __init__(self, ref):
        self.ref = ref


def test_fixture_source_accepted():
    mark = requires(source=Fixture("id_name"), access="rw")
    req = mark.args[0]
    assert isinstance(req, Requirement)
    assert isinstance(req.source, Fixture)


def test_string_source_accepted():
    mark = requires(source="cat.schema.tbl", access="ro")
    req = mark.args[0]
    assert req.source == "cat.schema.tbl"
    assert req.resolved_name() == "tbl"


def test_empty_string_source_rejected():
    with pytest.raises(ValueError, match="Fixture"):
        requires(source="", access="ro")


def test_none_source_rejected():
    with pytest.raises(ValueError, match="Fixture"):
        requires(source=None, access="ro")


def test_lazy_ref_source_requires_name():
    with pytest.raises(ValueError, match="name="):
        requires(source=_LazyRefStub("default/x"), access="ro")


def test_lazy_ref_source_with_name_accepted():
    mark = requires(source=_LazyRefStub("default/x"), access="ro", name="x")
    req = mark.args[0]
    assert isinstance(req.source, _LazyRefStub)
    assert req.resolved_name() == "x"  # from name=, never falls to the string-splitting fallback
