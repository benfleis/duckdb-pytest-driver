"""Spine demonstration tests for the redesign: the binary-authoritative reconcile (false-green
killer) and the typed registry/plan that replace the shipped predictor + `config._ATTR` sprawl.

Pure-logic: no duckdb binary, no docker, no xdist. Proves the new modules import and behave.
"""

import os

import pytest

from ducktest import collect
from ducktest.context import Plan, Registry, SessionContext


# --- binary-authoritative collection: verify mode refuses false-green -------------------------


def test_verify_mode_errors_when_binary_knows_a_test_the_fs_missed():
    # the exact false-green case: a `.test_slow` the FS gate drops but the binary registers.
    fs = {"a.test", "b.test"}
    binary = {"a.test", "b.test", "slow/big.test_slow"}
    rec = collect.reconcile(fs, binary, mode="verify")
    assert rec.only_in_binary == frozenset({"slow/big.test_slow"})
    with pytest.raises(RuntimeError, match="false-green"):
        rec.check()


def test_verify_mode_passes_when_fs_and_binary_agree():
    names = {"a.test", "b.test"}
    rec = collect.reconcile(names, names, mode="verify")
    rec.check()  # no raise
    assert rec.collected_names == frozenset(names)


def test_authoritative_mode_collects_the_union_without_erroring():
    fs = {"a.test", "local_only.test"}
    binary = {"a.test", "registered_only.test"}
    rec = collect.reconcile(fs, binary, mode="authoritative")
    rec.check()  # authoritative never raises
    assert rec.collected_names == frozenset({"a.test", "local_only.test", "registered_only.test"})


def test_plan_divergence_surfaces_the_false_green_set():
    plan = Plan(fs_names=frozenset({"a.test"}), binary_names=frozenset({"a.test", "b.test"}))
    assert plan.collection_divergence == frozenset({"b.test"})


# --- the member/role model --------------------------------------------------------------------


def test_role_model_pairs_by_stem(tmp_path):
    body = tmp_path / "x.test"
    body.write_text("# body\n")
    assert not collect.has_driver(str(body))  # no sibling .py yet
    (tmp_path / "x.py").write_text("# driver\n")
    assert collect.has_driver(str(body))  # now the body is driver-suppressed
    assert collect.is_driver(str(tmp_path / "x.py"))


# --- the typed registry replaces four config._ATTR string maps --------------------------------


class _Suite:
    def __init__(self, name):
        self.name = name


def test_registry_rejects_duplicate_suite():
    reg = Registry()
    reg.register_suite(_Suite("cloud"))
    with pytest.raises(ValueError, match="duplicate suite"):
        reg.register_suite(_Suite("cloud"))


def test_scoped_registry_resolves_nearest_ancestor(tmp_path):
    root = tmp_path
    ext = tmp_path / "ext_a"
    ext.mkdir()
    reg = Registry()
    reg.register_provisioner(str(root), "root-prov")
    reg.register_provisioner(str(ext), "ext-prov")
    # a path under ext_a resolves the more-specific provisioner, not the root last-wins
    assert reg.provisioner_for(str(ext / "test" / "t.py")) == "ext-prov"
    # a path elsewhere under root falls back to root
    assert reg.provisioner_for(str(root / "other" / "t.py")) == "root-prov"
    # an unrelated path resolves nothing (no path=None back-compat branch)
    assert reg.provisioner_for(os.sep + "unrelated") is None


def test_session_context_convenience_lookups():
    ctx = SessionContext(working_dir="/wd", run_id="2026-07-18T00-00-00Z--brave-otter")
    ctx.registry.register_suite(_Suite("cloud"))
    assert ctx.suite("cloud").name == "cloud"
    assert ctx.suite("missing") is None
    assert ctx.existing_services == {}  # empty by default


# --- --emit-plan: the plan-as-artifact serialization ------------------------------------------


def test_plan_as_dict_is_json_able_sorted_and_surfaces_divergence():
    import json

    plan = Plan(
        selected_nodeids=frozenset({"b.py::t", "a.py::t"}),
        reachable_suites=frozenset({"cloud"}),
        needed_services=frozenset({"minio"}),
        needed_credentials=frozenset({"onepw"}),
        fs_names=frozenset({"a.test"}),
        binary_names=frozenset({"a.test", "slow/big.test_slow"}),
    )
    d = plan.as_dict()
    assert d["selected_nodeids"] == ["a.py::t", "b.py::t"]  # sorted, deterministic
    assert d["needed_services"] == ["minio"] and d["needed_credentials"] == ["onepw"]
    assert d["collection"]["divergence"] == ["slow/big.test_slow"]  # the false-green name is visible
    json.dumps(d)  # round-trips cleanly
