"""The test environment: what one test is made of, where each part came from, and what is masked."""

import re

import pytest
from test_ducktest5_run import StoreDef

from ducktest5 import leases
from ducktest5.binary import UsageError
from ducktest5.declare import Variant
from ducktest5.model import SQLLOGIC, Test, TestDef
from ducktest5.resources import RO, RW, S3CredentialDef, StorageDef
from ducktest5.resources import credential as cred_mod
from ducktest5.resources import from_env
from ducktest5.resources.made import Made
from ducktest5.sqllogic import init_file_text
from ducktest5.scan import scan
from ducktest5.testenv import resolve


def make_test(axis_values=None, *, params_key=None, sources=None, inputs=(), **fields):
    definition = TestDef(path="t.test", name="t", type=SQLLOGIC, body_file="t.test", **fields)
    definition.sources = sources or {}
    return Test(definition, params_key, axis_values or {}, tuple((d, RW) for d in inputs))


def test_the_axis_value_reaches_the_body_as_a_value_without_the_author_repeating_it():
    env = resolve(make_test({"storage": Variant("s3")}))
    assert env.value_map == {"storage": "s3"}
    assert env.names == {"STORAGE": "s3"}
    assert env.values["storage"].source == "AXES storage"


def test_values_merge_rules_then_the_definition_then_the_params_and_the_variant_adds_its_own():
    c = make_test(
        {"storage": Variant("s3", values={"where": "variant"})},
        params_key="wide",
        params={"wide": {"who": "row", "width": 9}},
        values={"who": "rule", "region": "eu"},
        sources={("values", "region"): "ducktest_conf.py: default"},
    )
    env = resolve(c)
    assert env.value_map == {"storage": "s3", "who": "row", "region": "eu", "width": 9, "where": "variant"}
    assert env.values["region"].source == "ducktest_conf.py: default"
    assert env.values["width"].source == "params 'wide'"


@pytest.mark.parametrize(
    "fields, other",
    [
        ({"params_key": "wide", "params": {"wide": {"storage": "x"}}}, "the params row 'wide'"),
        (
            {"values": {"STORAGE": "x"}, "sources": {("values", "STORAGE"): "ducktest_conf.py: default"}},
            "ducktest_conf.py: default",
        ),
        ({"values": {"storage": "x"}}, "@test"),
    ],
)
def test_a_value_given_by_an_axis_value_and_by_a_rule_or_the_params_row_is_an_error_naming_both(fields, other):
    for axis_values in ({"storage": Variant("s3")}, {"auth": Variant("s3", values={"storage": "y"})}):
        where = "AXES storage=s3" if "storage" in axis_values else "AXES auth=s3"
        with pytest.raises(UsageError) as caught:
            resolve(make_test(axis_values, **fields))
        assert str(caught.value).endswith(
            f": the value 'storage' is given by {where} and by {other}; give it in one place"
        )


def test_two_axis_values_giving_one_name_is_a_scan_error():
    axis_values = {"a": Variant("one", values={"ROOT": "/a"}), "b": Variant("two", values={"ROOT": "/b"})}
    with pytest.raises(UsageError, match=r"'ROOT' is given by both AXES a=one and AXES b=two"):
        resolve(make_test(axis_values))


def test_a_rule_and_the_params_row_giving_one_name_is_fine_the_row_wins():
    c = make_test(params_key="wide", params={"wide": {"who": "row"}}, values={"who": "rule"})
    env = resolve(c)
    assert env.value_map == {"who": "row"}
    assert env.values["who"].source == "params 'wide'"


def test_a_setting_is_a_statement_after_the_requires_and_the_init_sql_comes_last():
    variant = Variant("s3", requires=("httpfs", "json"), settings={"s3_region": "eu"}, init_sql="CREATE SECRET s")
    env = resolve(make_test({"storage": variant}, settings={"threads": 2}))
    assert env.extensions == ["httpfs", "json"]
    assert env.sql == "SET threads = '2';\nSET s3_region = 'eu';\nCREATE SECRET s;"


def test_a_rule_loads_its_extensions_before_an_axis_value_and_once():
    env = resolve(make_test({"a": Variant("one", requires=("json", "httpfs"))}, requires=["httpfs"]))
    assert env.extensions == ["httpfs", "json"]


def test_an_extension_asked_for_twice_is_loaded_once_in_the_order_of_the_axes():
    axis_values = {"a": Variant("one", requires=("httpfs",)), "b": Variant("two", requires=("json", "httpfs"))}
    assert resolve(make_test(axis_values)).extensions == ["httpfs", "json"]


def test_the_environment_of_the_engine_process_is_this_process_with_the_variant_on_top():
    variant = Variant("no-key", process_env={"AWS_PROFILE": "ducktest", "PATH": None})
    env = resolve(make_test({"auth": variant}))
    assert env.environ({"PATH": "/bin", "HOME": "/home/me"}) == {"HOME": "/home/me", "AWS_PROFILE": "ducktest"}


def test_an_engine_process_gets_no_sdk_name_of_the_shells_env_but_what_inherit_env_keeps(monkeypatch):
    for name, value in (("AWS_PROFILE", "me"), ("AWS_SECRET_ACCESS_KEY", "mine"), ("AWS_CA_BUNDLE", "/ca"), ("X", "1")):
        monkeypatch.setenv(name, value)
    environ = resolve(make_test()).environ()
    assert ("X" in environ, [n for n in environ if n.startswith("AWS_")]) == (True, [])
    monkeypatch.setattr(cred_mod, "_inherit", ())
    cred_mod.inherit_env(["AWS_CA_BUNDLE"])
    assert [n for n in resolve(make_test()).environ() if n.startswith("AWS_")] == ["AWS_CA_BUNDLE"]
    assert cred_mod.removed_names() == ["AWS_PROFILE", "AWS_SECRET_ACCESS_KEY"]


def test_a_key_in_inherit_env_is_a_usage_error():
    message = (
        "INHERIT_ENV: AWS_SECRET_ACCESS_KEY is a credential name; declare S3CredentialDef.from_aws_env(...) instead"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        cred_mod.inherit_env(["AWS_SECRET_ACCESS_KEY"])


def test_an_unset_removes_what_an_earlier_axis_set():
    sets_x = Variant("sets", process_env={"X": "1"})
    unsets_x = Variant("unsets", process_env={"X": None})

    later_wins = resolve(make_test({"a": sets_x, "b": unsets_x}))
    assert "X" not in later_wins.environ({"HOME": "/home"})

    earlier_wins = resolve(make_test({"a": unsets_x, "b": sets_x}))
    assert earlier_wins.environ({"HOME": "/home"}) == {"HOME": "/home", "X": "1"}


def test_a_name_that_says_credential_is_masked():
    variant = Variant("s3", process_env={"AWS_SECRET_ACCESS_KEY": "hunter2"})
    env = resolve(make_test({"storage": variant}, values={"token": "s3cr3t"}))
    printed = "\n".join(env.lines())
    assert "values: token=***" in printed and "process env: AWS_SECRET_ACCESS_KEY=***" in printed
    assert "s3cr3t" not in printed and "hunter2" not in printed
    assert env.process_env["AWS_SECRET_ACCESS_KEY"].value == "hunter2"  # masked for a reader, delivered whole


def test_a_secret_named_value_is_masked_however_it_was_given():
    via_values = resolve(make_test({"a": Variant("v", values={"AWS_SECRET_ACCESS_KEY": "s3cr3t-1"})}))
    via_env = resolve(make_test({"a": Variant("v", process_env={"AWS_SECRET_ACCESS_KEY": "s3cr3t-2"})}))
    via_settings = resolve(make_test({"a": Variant("v", settings={"AWS_SECRET_ACCESS_KEY": "s3cr3t-3"})}))
    for env, secret in ((via_values, "s3cr3t-1"), (via_env, "s3cr3t-2"), (via_settings, "s3cr3t-3")):
        printed = "\n".join(env.lines())
        assert "AWS_SECRET_ACCESS_KEY=***" in printed
        assert secret not in printed
        assert secret not in "\n".join(r["is"] for r in env.record())


def test_an_unset_is_never_masked():
    env = resolve(make_test({"a": Variant("v", process_env={"AWS_SECRET_ACCESS_KEY": None})}))
    assert "process env unset: AWS_SECRET_ACCESS_KEY  <- AXES a=v" in env.lines()


def test_init_sql_is_named_by_its_source_and_never_printed():
    env = resolve(make_test({"storage": Variant("s3", init_sql="CREATE SECRET (SECRET 'hunter2')")}))
    assert env.lines() == ["init sql  <- AXES storage=s3", "values: storage=s3  <- AXES storage"]


def test_two_tests_share_a_key_only_when_everything_the_process_gets_is_the_same():
    plain = Variant("plain")
    same = resolve(make_test({"k": plain}, values={"region": "eu"})).key()
    assert resolve(make_test({"k": plain}, values={"region": "eu"})).key() == same
    for other in (
        make_test({"k": plain}, values={"region": "us"}),
        make_test({"k": Variant("plain", requires=("httpfs",))}, values={"region": "eu"}),
        make_test({"k": Variant("plain", process_env={"AWS_PROFILE": "p"})}, values={"region": "eu"}),
        make_test({"k": Variant("plain", process_env={"AWS_PROFILE": None})}, values={"region": "eu"}),
        make_test({"k": plain}, values={"region": "eu"}, inputs=[StoreDef()]),
    ):
        assert resolve(other).key() != same


def test_the_key_does_not_change_with_the_rule_that_gave_a_value():
    plain = make_test(values={"region": "eu"})
    named = make_test(values={"region": "eu"}, sources={("values", "region"): "ducktest_conf.py: default"})
    assert resolve(plain).key() == resolve(named).key()


def test_a_service_input_gives_its_attributes_as_values_once_it_is_up_and_never_its_secrets():
    test = make_test(inputs=[StoreDef()])
    assert "values: <from service store>  <- service store" in resolve(test).lines()
    made = Made("r1")
    made.create(StoreDef())
    env = resolve(test, made)
    assert env.value_map == {"STORE_ENDPOINT": "http://store"}
    assert "s3cr3t" not in "\n".join(env.lines()) and not env.process_env


def test_a_value_names_an_attribute_of_an_input_or_of_its_input_filled_when_the_test_runs():
    store = StoreDef()
    on_store = StorageDef("lake", "s3://lake", inputs=[store])
    test = make_test(inputs=[on_store], values={"EP": "{store.endpoint}/x", "OTHER": "{elsewhere.endpoint}"})
    assert resolve(test).value_map["EP"] == "{store.endpoint}/x"
    made = Made("r1")
    made.create(store)
    env = resolve(test, made)
    assert env.value_map["EP"] == "http://store/x"
    assert env.value_map["OTHER"] == "{elsewhere.endpoint}", "not an input of the test: left as written"
    with pytest.raises(UsageError, match=r"\{store.token\}: store has no attribute 'token'"):  # a secret is not one
        resolve(make_test(inputs=[on_store], values={"T": "{store.token}"}), made)


def test_a_python_body_adds_values_and_sql_for_the_test_file_beside_it():
    env = resolve(make_test({"storage": Variant("s3", init_sql="SET a = 1")}))
    more = env.plus(values={"TABLE": "id_name"}, init_sql="CREATE SECRET s")
    assert more.sql == "SET a = 1;\nCREATE SECRET s;"
    assert more.names["TABLE"] == "id_name"
    assert "values: TABLE=id_name  <- ctx.run_paired()" in more.lines()
    assert env.sql == "SET a = 1;" and "TABLE" not in env.names  # the test's own is untouched


def test_a_test_file_creates_its_scoped_secret_from_a_template_the_binary_fills(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k-value")
    monkeypatch.setenv("DT_SECRET", "s-value")
    storage = StorageDef("bucket", "s3://bucket")
    credential = S3CredentialDef(
        "key",
        sources=[from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})],
        scopes=["bucket"],
    )
    test = make_test()
    test.inputs = ((storage, RW), (credential, RO))
    resolved = resolve(test)
    text, _ = init_file_text(resolved)
    assert text == (
        "require httpfs\n\nstatement ok\n"
        "CREATE SECRET ducktest_key (TYPE s3, KEY_ID '{AWS_ACCESS_KEY_ID}', SECRET '{AWS_SECRET_ACCESS_KEY}', "
        "SCOPE 's3://bucket');\n"
    )
    assert resolved.passthrough == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    assert "k-value" not in "\n".join(resolved.lines()) + text


def test_the_scoped_secret_of_a_test_file_does_not_change_its_key(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k-value")
    monkeypatch.setenv("DT_SECRET", "s-value")
    storage = StorageDef("bucket", "s3://bucket")
    credential = S3CredentialDef(
        "key", sources=[from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})], scopes=["bucket"]
    )
    sql_file = make_test()
    python = Test(TestDef(path="t.py", name="t", type="python"), None, {})
    for test in (sql_file, python):
        test.inputs = ((storage, RW), (credential, RO))
    assert resolve(sql_file).secret_requires and not resolve(python).secret_requires
    assert resolve(sql_file).key() == resolve(python).key()


@pytest.mark.parametrize("held, scoped", [("credential", True), ("both", False)])
def test_no_secret_is_rendered_without_a_storage_in_the_scope_and_the_env_still_is(monkeypatch, held, scoped):
    monkeypatch.setenv("DT_KEY", "k-value")
    monkeypatch.setenv("DT_SECRET", "s-value")
    storage = StorageDef("bucket", "s3://bucket")
    keys = from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})
    credential = S3CredentialDef("key", sources=[keys], scopes=["bucket"] if scoped else [])
    test = make_test()
    test.inputs = ((credential, RO),) if held == "credential" else ((storage, RW), (credential, RO))
    env = resolve(test)
    assert (env.secret_sql, env.secret_requires) == ([], [])
    assert "secret: none (no storage of the test is in its scope)  <- credential key" in env.lines()
    assert env.environ({})["AWS_ACCESS_KEY_ID"] == "k-value"


# -----------------------------------------------------------------------------
# The default storage, declared
#


def test_binding_the_declared_default_storage_gives_what_binding_none_gives(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest5.resources import default_storage\nSTORAGES = [default_storage()]\n"
    )
    (tmp_path / "t.py").write_text(
        "from ducktest5 import test\n\n\n@test(storage='default')\ndef bound(ctx):\n    pass\n\n\n"
        "@test()\ndef plain(ctx):\n    pass\n"
    )
    tests, project = scan(str(tmp_path), working_dir=str(tmp_path))
    by_name = {t.definition.name: t for t in tests}
    assert [d.id for d in project.storages.values()] == ["default"]
    assert by_name["bound"].inputs == by_name["plain"].inputs == ()

    def given(test, key):
        env = resolve(test, Made("r1"), key=key, default=project.default_storage)
        return env.record(), leases.planned(test, env, project)

    for key in ("k", None):  # a Python body's own place, and the run's temp root a .test body gets
        assert given(by_name["bound"], key) == given(by_name["plain"], key)


def test_a_value_read_at_delivery_is_printed_as_its_value_not_its_getter():
    from ducktest5.testenv import Entry, TestEnv

    env = TestEnv()
    env.process_env["AWS_REGION"] = Entry(lambda: "eu-west-1", "credential keys", kind="env", name="AWS_REGION")
    assert env.lines() == ["process env: AWS_REGION=eu-west-1  <- credential keys"]
