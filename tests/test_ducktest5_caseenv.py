"""The case environment: what one case is made of, where each part came from, and what is masked."""

import pytest

from ducktest5.binary import UsageError
from ducktest5.caseenv import resolve
from ducktest5.declare import Service, Variant
from ducktest5.model import SQLLOGIC, Case, TestDef
from ducktest5.provision import EXISTING, Provisioned

STORE = Service(
    "store",
    block=lambda **o: {"endpoint": "http://store", "secret_key": "s3cr3t"},
    alive=lambda block: True,
    secrets=("secret_key",),
)


def provisioned():
    return {"store": Provisioned(STORE, STORE.block(), EXISTING)}


def case(axis_values=None, *, case_id=None, sources=None, **fields):
    test = TestDef(path="t.test", name="t", type=SQLLOGIC, body_file="t.test", **fields)
    test.sources = sources or {}
    return Case(test, case_id, axis_values or {})


def test_the_axis_value_reaches_the_body_as_a_value_without_the_author_repeating_it():
    env = resolve(case({"storage": Variant("s3")}))
    assert env.value_map == {"storage": "s3"}
    assert env.names == {"STORAGE": "s3"}
    assert env.values["storage"].source == "AXES storage"


def test_values_merge_rules_then_the_test_then_the_case_then_the_variant():
    c = case(
        {"storage": Variant("s3", values={"who": "variant"})},
        case_id="wide",
        cases={"wide": {"who": "case", "width": 9}},
        values={"who": "rule", "region": "eu"},
        sources={("values", "region"): "ducktest_conf.py: default"},
    )
    env = resolve(c)
    assert env.value_map == {"storage": "s3", "who": "variant", "region": "eu", "width": 9}
    assert env.values["region"].source == "ducktest_conf.py: default"
    assert env.values["width"].source == "case 'wide'"


def test_a_setting_is_a_statement_after_the_requires_and_the_init_sql_comes_last():
    variant = Variant("s3", requires=("httpfs", "json"), settings={"s3_region": "eu"}, init_sql="CREATE SECRET s")
    env = resolve(case({"storage": variant}, settings={"threads": 2}))
    assert env.extensions == ["httpfs", "json"]
    assert env.sql == "SET threads = '2';\nSET s3_region = 'eu';\nCREATE SECRET s;"


def test_an_extension_asked_for_twice_is_loaded_once_in_the_order_of_the_axes():
    axis_values = {"a": Variant("one", requires=("httpfs",)), "b": Variant("two", requires=("json", "httpfs"))}
    assert resolve(case(axis_values)).extensions == ["httpfs", "json"]


def test_a_root_the_binary_does_not_have_is_refused():
    with pytest.raises(UsageError, match="root 'tmp' is not one of"):
        resolve(case({"storage": Variant("s3", roots={"tmp": "/x"})}))


def test_what_a_variant_makes_out_of_a_service_waits_for_provisioning():
    variant = Variant(
        "s3",
        services=("store",),
        values=lambda blocks: {"root": blocks["store"]["endpoint"]},
        init_sql=lambda blocks: f"ATTACH '{blocks['store']['endpoint']}'",
    )
    before = resolve(case({"storage": variant}))
    assert "root" not in before.value_map
    assert "values: <from service store>  <- AXES storage=s3" in before.lines()
    after = resolve(case({"storage": variant}), provisioned())
    assert after.value_map["root"] == "http://store"
    assert after.sql == "ATTACH 'http://store';"
    assert "service: store (existing)  <- AXES storage=s3" in after.lines()


def test_the_environment_of_the_engine_process_is_this_process_with_the_variant_on_top():
    variant = Variant("no-key", env={"AWS_PROFILE": "ducktest"}, env_unset=("PATH",))
    env = resolve(case({"auth": variant}))
    assert env.changes_environment
    assert env.environ({"PATH": "/bin", "HOME": "/home/me"}) == {"HOME": "/home/me", "AWS_PROFILE": "ducktest"}
    assert resolve(case()).environ() is None  # nothing to change: the engine keeps ours


def test_a_secret_of_a_service_and_a_name_that_says_credential_are_masked():
    variant = Variant("s3", services=("store",), env={"AWS_SECRET_ACCESS_KEY": "hunter2"})
    env = resolve(case({"storage": variant}, values={"token": "s3cr3t"}), provisioned())
    printed = "\n".join(env.lines())
    assert "values: token=***" in printed and "env: AWS_SECRET_ACCESS_KEY=***" in printed
    assert "s3cr3t" not in printed and "hunter2" not in printed
    assert env.env["AWS_SECRET_ACCESS_KEY"].value == "hunter2"  # masked for a reader, delivered whole


def test_init_sql_is_named_by_its_source_and_never_printed():
    env = resolve(case({"storage": Variant("s3", init_sql="CREATE SECRET (SECRET 'hunter2')")}))
    assert env.lines() == ["init sql  <- AXES storage=s3", "values: storage=s3  <- AXES storage"]


def test_two_cases_share_a_key_only_when_everything_the_process_gets_is_the_same():
    plain = Variant("plain")
    same = resolve(case({"k": plain}, values={"region": "eu"})).key()
    assert resolve(case({"k": plain}, values={"region": "eu"})).key() == same
    for other in (
        case({"k": plain}, values={"region": "us"}),
        case({"k": Variant("plain", requires=("httpfs",))}, values={"region": "eu"}),
        case({"k": Variant("plain", roots={"data_dir": "/d"})}, values={"region": "eu"}),
        case({"k": Variant("plain", env={"AWS_PROFILE": "p"})}, values={"region": "eu"}),
        case({"k": Variant("plain", env_unset=("AWS_PROFILE",))}, values={"region": "eu"}),
        case({"k": plain}, values={"region": "eu"}, services=["store"]),
    ):
        assert resolve(other).key() != same


def test_the_key_does_not_change_with_the_rule_that_gave_a_value():
    plain = case(values={"region": "eu"})
    named = case(values={"region": "eu"}, sources={("values", "region"): "ducktest_conf.py: default"})
    assert resolve(plain).key() == resolve(named).key()


def test_a_service_that_did_not_come_up_leaves_its_parts_pending_not_called():
    variant = Variant(
        "s3",
        services=("store",),
        values=lambda blocks: {"root": blocks["store"]["endpoint"]},
        init_sql=lambda blocks: f"ATTACH '{blocks['store']['endpoint']}'",
    )
    env = resolve(case({"storage": variant}), provisioned={})  # provisioning ran; store is not in it
    assert "root" not in env.value_map
    assert "values: <from service store>  <- AXES storage=s3" in env.lines()
    assert "service: store (did not come up)  <- AXES storage=s3" in env.lines()


def test_a_callable_that_raises_is_a_usage_error_naming_the_variant():
    def boom(blocks):
        raise RuntimeError("no bucket configured")

    variant = Variant("s3", services=("store",), values=boom)
    with pytest.raises(UsageError, match=r"AXES storage=s3.*values.*no bucket configured"):
        resolve(case({"storage": variant}), provisioned())


def test_a_python_body_adds_values_and_sql_for_the_test_file_beside_it():
    env = resolve(case({"storage": Variant("s3", init_sql="SET a = 1")}))
    more = env.plus(values={"TABLE": "id_name"}, init_sql="CREATE SECRET s")
    assert more.sql == "SET a = 1;\nCREATE SECRET s;"
    assert more.names["TABLE"] == "id_name"
    assert "values: TABLE=id_name  <- ctx.run_test()" in more.lines()
    assert env.sql == "SET a = 1;" and "TABLE" not in env.names  # the case's own is untouched
