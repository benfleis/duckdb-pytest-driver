"""Resources: identity by content, ownership against sharing, attributes, and the handler contract."""

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass

import pytest

from ducktest5.binary import UsageError
from ducktest5.cli import main
from ducktest5.resources import (
    EXCLUSIVE,
    NEVER,
    RO,
    RW,
    SHARED_RO,
    SHARED_RW,
    Attribute,
    CredentialDef,
    Field,
    Resource,
    S3Credential,
    S3CredentialDef,
    StorageDef,
    check_contract,
    from_env,
    pick_credentials,
    local_path,
    scheme_of,
)
from ducktest5.resources import credential as credential_module
from ducktest5.scan import scan
from ducktest5.testenv import resolve

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")

# A small kind, as source text, so that another process can declare the very same one.
THING = """
import os
from dataclasses import dataclass, field
from ducktest5.resources import ResourceDef

@dataclass(eq=False)
class ThingDef(ResourceDef):
    kind = "thing"
    id: str
    size: int = 1
    make_path: object = os.path.join
    inputs: list = field(default_factory=list)
    retain: str = "on_failure"
"""
_space = {}
exec(THING, _space)
ThingDef = _space["ThingDef"]


# -----------------------------------------------------------------------------
# Identity
#


def test_identity_is_the_same_in_another_process():
    code = THING + "print(ThingDef('a', size=3).identity())"
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert other.stdout.strip() == ThingDef("a", size=3).identity()


def test_identity_is_content_the_name_is_a_label():
    assert ThingDef("a").identity() == ThingDef("b").identity()
    assert ThingDef("a", size=1).identity() != ThingDef("a", size=2).identity()


def test_identity_changes_with_an_input_identity_and_its_mode():
    d = ThingDef("a", inputs=["up"])
    assert d.identity({"up": "1"}) != d.identity({"up": "2"})
    assert ThingDef("a", inputs=[{"up": RO}]).identity({"up": "1"}) != d.identity({"up": "1"})


def test_an_input_not_fulfilled_has_no_identity():
    with pytest.raises(ValueError, match=r"inputs \['up'\] are not fulfilled"):
        ThingDef("a", inputs=["up"]).identity()


def test_an_input_may_be_a_def_object():
    up = ThingDef("up")
    assert ThingDef("a", inputs=[up]).edges() == [("up", RW, up)]
    assert ThingDef("a", inputs=[{up: RO}]).edges() == [("up", RO, up)]


def test_the_acquire_mode_splits_identity():
    d = ThingDef("a")
    assert d.identity(mode=RW) != d.identity(mode=RO)


def test_make_gives_the_instance_the_identity_of_its_fulfilled_inputs():
    up = ThingDef("up").make(run_token="run1")
    a = ThingDef("a", inputs=["up"]).make({"up": up}, run_token="run1")
    assert a.identity == ThingDef("a", inputs=["up"]).identity({"up": up.identity})
    assert a.run_token == "run1" and a.inputs == {"up": up}


# -----------------------------------------------------------------------------
# Ownership
#


def _kind(sharing, mint=None):
    class Instance(Resource):
        def mint_child(self, key):
            return mint(self, key) if mint else None

    return type("K", (ThingDef,), {"sharing": sharing, "instance": Instance})


def test_a_writer_of_an_exclusive_kind_waits_for_the_one_before_it():
    thing = _kind(EXCLUSIVE)("a").make()
    held = thing.acquire(RW, "t1")
    assert held is thing
    order = []

    def second():
        thing.release(thing.acquire(RW, "t2"), RW)
        order.append("second")

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.1)
    order.append("first")
    thing.release(held, RW)
    t.join(2)
    assert order == ["first", "second"]


def test_readers_of_an_exclusive_kind_share_it():
    thing = _kind(EXCLUSIVE)("a").make()
    assert thing.acquire(RO) is thing and thing.acquire(RO) is thing


def test_an_exclusive_writer_gets_a_minted_child_per_key_instead_of_a_lock():
    kind = _kind(EXCLUSIVE, mint=lambda parent, key: ThingDef("c").make(run_token=parent.run_token))
    thing = kind("a").make(run_token="run1")
    one, two = thing.acquire(RW, "t1"), thing.acquire(RW, "t2")
    assert one is not thing and one is not two
    assert thing.acquire(RW, "t1") is one
    assert thing.children == {"t1": one, "t2": two}


def test_a_read_only_kind_serves_a_writer_as_a_reader():
    thing = _kind(SHARED_RO)("a").make()
    assert thing.acquire(RW) is thing and thing.acquire(RW) is thing


def test_retain_says_when_the_run_leaves_it_in_place():
    assert ThingDef("a").make().retained(failed=True)
    assert not ThingDef("a").make().retained(failed=False)
    assert not ThingDef("a", retain=NEVER).make().retained(failed=True)


# -----------------------------------------------------------------------------
# Outputs
#


def test_attributes_are_named_under_the_producer_and_delivered_as_the_kind_names_them():
    thing = ThingDef("a").make()
    thing.attributes["ENDPOINT"] = Attribute("http://x", "thing a")
    thing.secrets["KEY"] = Attribute(lambda: "k", "thing a")
    assert set(thing.by_ref()) == {"a.ENDPOINT", "a.KEY"}
    env, secrets = thing.delivery()
    assert env["ENDPOINT"].get() == "http://x" and secrets["KEY"].get() == "k"


def test_an_output_of_none_unsets_the_name():
    assert Attribute(None, "x").unset and not Attribute("", "x").unset


# -----------------------------------------------------------------------------
# The contract helper
#


def test_the_contract_helper_fails_a_kind_whose_delete_of_a_missing_instance_raises():
    class Fragile(Resource):
        def dealloc(self):
            raise FileNotFoundError("not there")

    kind = type("F", (ThingDef,), {"instance": Fragile})
    with pytest.raises(FileNotFoundError):
        check_contract(lambda run_token: kind("a").make(run_token=run_token))


def test_the_contract_helper_passes_the_base_kind():
    check_contract(lambda run_token: ThingDef("a").make(run_token=run_token))


# -----------------------------------------------------------------------------
# Credentials
#

KEYS = {"DT_KEY": "key_id", "DT_SECRET": "secret"}


@dataclass(eq=False)
class TokenCredentialDef(CredentialDef):
    """A credential class of the tests' own: one token."""

    type = "token"
    fields = {"token": Field(required=True, secret=True)}
    env = {"token": ("DT_TOKEN",)}


def test_a_credential_takes_the_first_source_that_resolves(monkeypatch):
    monkeypatch.delenv("DT_MISSING", raising=False)
    monkeypatch.delenv("DT_MISSING_SECRET", raising=False)
    monkeypatch.setenv("DT_KEY", "k1")
    monkeypatch.setenv("DT_SECRET", "s1")
    missing = from_env({"DT_MISSING": "key_id", "DT_MISSING_SECRET": "secret"})
    cred = S3CredentialDef("key", sources=[missing, from_env(KEYS)]).make()
    cred.create()
    assert cred.attributes == {}
    assert cred.secrets["AWS_ACCESS_KEY_ID"].source == "env DT_KEY"
    monkeypatch.setenv("DT_KEY", "k2")  # read when delivered, not when resolved
    assert cred.delivery()[1]["AWS_ACCESS_KEY_ID"].get() == "k2"


def test_an_s3_credential_def_makes_an_s3_credential(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    monkeypatch.setenv("DT_SECRET", "s")
    assert isinstance(S3CredentialDef("key", sources=[from_env(KEYS)]).make(), S3Credential)


def test_sources_never_mix(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k1")
    monkeypatch.delenv("DT_MISSING_SECRET", raising=False)
    monkeypatch.setenv("DT_SECRET", "s2")
    monkeypatch.setenv("DT_REGION", "eu-west-1")
    first = from_env({"DT_KEY": "key_id", "DT_MISSING_SECRET": "secret", "DT_REGION": "region"})
    cred = S3CredentialDef("key", sources=[first, from_env(KEYS)]).make()
    cred.create()
    assert sorted(cred.fields) == ["key_id", "secret"], "the region of the first source does not come along"


def test_an_optional_field_that_is_not_set_is_left_out(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    monkeypatch.setenv("DT_SECRET", "s")
    monkeypatch.delenv("DT_REGION", raising=False)
    cred = S3CredentialDef("key", sources=[from_env({**KEYS, "DT_REGION": "region"})]).make()
    cred.create()
    assert sorted(cred.secrets) == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]


def test_a_region_is_delivered_under_both_names_and_read_from_the_first_set(monkeypatch):
    for name, value in (("AWS_ACCESS_KEY_ID", "k"), ("AWS_SECRET_ACCESS_KEY", "s"), ("AWS_DEFAULT_REGION", "d")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    cred = S3CredentialDef.from_aws_env("aws").make()
    cred.create()
    assert {n: a.get() for n, a in cred.secrets.items()} == {
        "AWS_ACCESS_KEY_ID": "k",
        "AWS_SECRET_ACCESS_KEY": "s",
        "AWS_REGION": "d",
        "AWS_DEFAULT_REGION": "d",
    }
    monkeypatch.setenv("AWS_REGION", "r")
    cred.create()
    assert cred.fields["region"].source == "env AWS_REGION"


def test_a_literal_is_a_default_a_source_overrides(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    monkeypatch.setenv("DT_SECRET", "s")
    monkeypatch.setenv("DT_REGION", "from-env")
    cred = S3CredentialDef("key", sources=[from_env(KEYS)], region="literal").make()
    cred.create()
    assert cred.fields["region"].get() == "literal"
    cred = S3CredentialDef("key", sources=[from_env({**KEYS, "DT_REGION": "region"})], region="literal").make()
    cred.create()
    assert cred.fields["region"].get() == "from-env"


def test_a_credential_none_of_whose_sources_resolve_says_which_it_tried(monkeypatch):
    monkeypatch.delenv("DT_MISSING", raising=False)
    with pytest.raises(UsageError, match=r"'key': none of its sources resolved: \{'env': \{'DT_MISSING'"):
        TokenCredentialDef("key", sources=[from_env({"DT_MISSING": "token"})]).make().create()


def test_1password_is_not_available_here(monkeypatch):
    monkeypatch.delenv("DT_TOKEN", raising=False)
    cred = TokenCredentialDef("key", sources=["env:DT_TOKEN", "1password:ducktest-aws"]).make()
    with pytest.raises(UsageError, match=r"'key': source 1password:ducktest-aws is not available here"):
        cred.create()


@pytest.mark.parametrize(
    "make, message",
    [
        (
            lambda: S3CredentialDef("fake_key", sources=[from_env({"LADDER_FAKE_KEY": "key", "S": "secret"})]),
            "S3CredentialDef('fake_key'): from_env maps LADDER_FAKE_KEY to 'key', which is not a field of "
            "S3CredentialDef (key_id, secret, session_token, region)",
        ),
        (
            lambda: S3CredentialDef("fake_key", sources=[from_env({"LADDER_FAKE_KEY_ID": "key_id"})]),
            "S3CredentialDef('fake_key'): source from_env({'LADDER_FAKE_KEY_ID': 'key_id'}) gives no 'secret'; "
            "a source must give every required field (key_id, secret)",
        ),
        (
            lambda: S3CredentialDef("ci", sources=[from_env(["AWS_KEY"])]),
            "S3CredentialDef('ci'): from_env(['AWS_KEY']): AWS_KEY is not an env name of S3CredentialDef "
            "(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_REGION, AWS_DEFAULT_REGION); "
            "map it with from_env({'AWS_KEY': field})",
        ),
        (
            lambda: CredentialDef("k"),
            "CredentialDef('k'): CredentialDef is the base class; declare a typed credential, such as S3CredentialDef",
        ),
    ],
)
def test_a_credential_of_the_wrong_shape_is_a_usage_error(make, message):
    with pytest.raises(UsageError, match=re.escape(message)):
        make()


def test_a_credential_counts_by_its_type_and_source_never_its_value(monkeypatch):
    d = TokenCredentialDef("key", sources=["env:DT_TOKEN"])
    monkeypatch.setenv("DT_TOKEN", "one")
    before = d.identity()
    monkeypatch.setenv("DT_TOKEN", "two")
    assert d.identity() == before
    assert TokenCredentialDef("key", sources=[from_env({"DT_OTHER": "token"})]).identity() != before
    s3 = S3CredentialDef("key", sources=[from_env(KEYS)])
    assert s3.identity() != S3CredentialDef("key", sources=[from_env({**KEYS, "DT_REGION": "region"})]).identity()

    @dataclass(eq=False)
    class OtherTokenDef(TokenCredentialDef):
        type = "other"

    assert OtherTokenDef("key", sources=["env:DT_TOKEN"]).identity() != before


def test_a_callable_source_makes_the_fields_from_the_inputs_at_acquire(monkeypatch):
    monkeypatch.setenv("DT_TOKEN", "root")
    calls = []

    def token(inputs):
        calls.append(1)
        return {"token": "t-" + inputs["root"].fields["token"].get()}

    root = TokenCredentialDef("root", sources=["env:DT_TOKEN"]).make()
    root.create()
    cred = TokenCredentialDef("token", sources=[token], inputs=["root"]).make({"root": root})
    cred.create()
    assert calls == [] and cred.secrets == {}
    cred.acquire()
    assert cred.secrets["DT_TOKEN"].get() == "t-root"
    assert cred.secrets["DT_TOKEN"].source.startswith("callable ")
    assert callable(cred.secrets["DT_TOKEN"].value), "a value never counts toward an identity"


def test_a_callable_that_gives_an_unknown_field_is_a_usage_error():
    cred = TokenCredentialDef("token", sources=[lambda inputs: {"TOKEN": "x"}]).make()
    with pytest.raises(UsageError, match=r"gave \['TOKEN'\], not fields of TokenCredentialDef \(token\)"):
        cred.acquire()


def test_a_callable_source_returning_none_falls_through_to_the_next_source(monkeypatch):
    monkeypatch.setenv("DT_TOKEN", "k")
    calls = []

    def skip(inputs):
        calls.append(1)
        return None

    cred = TokenCredentialDef("key", sources=[skip, from_env(["DT_TOKEN"])]).make()
    cred.create()
    cred.acquire()
    assert calls == [1]
    assert cred.secrets["DT_TOKEN"].source == "env DT_TOKEN"


def test_env_colon_name_is_shorthand_for_env_of_one_name(monkeypatch):
    monkeypatch.setenv("DT_TOKEN", "k")
    cred = TokenCredentialDef("key", sources=["env:DT_TOKEN"]).make()
    cred.create()
    assert cred.secrets["DT_TOKEN"].source == "env DT_TOKEN"


def test_a_plain_string_source_that_is_not_a_reference_is_a_usage_error():
    with pytest.raises(UsageError, match="names where a secret lives"):
        TokenCredentialDef("key", sources=["oops"])


def test_a_credentials_values_are_plain_values_of_the_test(monkeypatch):
    monkeypatch.setenv("DT_TOKEN", "k")
    cred = TokenCredentialDef("key", sources=["env:DT_TOKEN"], values={"AZURE_AUTH_ENV": "1"}).make()
    cred.create()
    values, env = cred.delivery()
    assert {n: a.get() for n, a in values.items()} == {"AZURE_AUTH_ENV": "1"} and list(env) == ["DT_TOKEN"]


def test_a_credentials_default_retain_is_never():
    assert TokenCredentialDef("key").retain == NEVER


S3_KEY = S3CredentialDef("s3-key", scopes=["s3store"])
S3_OTHER = S3CredentialDef("s3-other", scopes=["s3store"])
S3_READ = S3CredentialDef("s3-read", scopes=[("s3store", RO)])
AZ_KEY = TokenCredentialDef("az-key", scopes=["azstore"])


def test_a_storage_gets_the_credential_scoped_to_it():
    assert pick_credentials({"s3store": RW, "azstore": RW}, [S3_KEY, AZ_KEY]) == [S3_KEY, AZ_KEY]


def test_a_read_only_credential_serves_a_read_only_edge_only():
    assert pick_credentials({"s3store": RW}, [S3_READ]) == []
    assert pick_credentials({"s3store": RO}, [S3_READ]) == [S3_READ]


def test_two_credentials_for_one_storage_none_named_is_a_usage_error_listing_them():
    with pytest.raises(UsageError, match=r"storage 's3store' has credentials \['s3-key', 's3-other'\]"):
        pick_credentials({"s3store": RW}, [S3_KEY, S3_OTHER])


def test_the_default_credential_breaks_the_tie():
    default = S3CredentialDef("s3-default", scopes=["s3store"], default=True)
    assert pick_credentials({"s3store": RW}, [S3_KEY, S3_OTHER, default]) == [default]


def test_a_named_credential_wins_over_scope_so_a_wrong_credential_test_can_be_declared():
    wrong = S3CredentialDef("wrong", scopes=["s3store"])
    none = TokenCredentialDef("none")
    assert pick_credentials({"s3store": RW}, [S3_KEY, S3_OTHER], named=[wrong]) == [wrong]
    assert pick_credentials({"s3store": RW}, [S3_KEY], named=[none]) == [none, S3_KEY]


def test_a_credential_scoped_to_two_storages_serves_both():
    both = S3CredentialDef("both", scopes=["s3store", "azstore"])
    assert pick_credentials({"s3store": RW, "azstore": RW}, [both]) == [both]


def test_a_storage_matched_by_two_credentials_still_errors_unless_one_is_named_or_default():
    both = S3CredentialDef("both", scopes=["s3store", "azstore"])
    with pytest.raises(UsageError, match=r"storage 's3store' has credentials \['both', 's3-key'\]"):
        pick_credentials({"s3store": RW, "azstore": RW}, [both, S3_KEY])


def test_a_credential_scoped_to_storage_a_is_not_delivered_for_a_test_that_uses_only_storage_b():
    assert pick_credentials({"azstore": RW}, [S3_KEY]) == []


def test_credential_contract(monkeypatch):
    monkeypatch.setenv("DT_TOKEN", "dt-token-value")
    check_contract(lambda run_token: TokenCredentialDef("key", sources=["env:DT_TOKEN"]).make(run_token=run_token))


# -----------------------------------------------------------------------------
# Storages
#


@pytest.mark.parametrize(
    "root, scheme",
    [
        ("/data", "file"),
        ("file:///data", "file"),
        ("s3://bucket", "s3"),
        ("az://container", "az"),
        ("abfss://c@acct.dfs.core.windows.net", "abfss"),
        ("https://host/x", "https"),
    ],
)
def test_the_scheme_is_the_roots(root, scheme):
    assert scheme_of(root) == scheme
    assert StorageDef("s", root).make().scheme == scheme


def test_an_unknown_scheme_is_a_usage_error():
    with pytest.raises(UsageError, match="scheme 'gs' is not one of"):
        StorageDef("s", "gs://bucket").make()


def test_a_local_storage_delivers_the_four_roots(tmp_path):
    env, secrets = StorageDef("local", str(tmp_path), data=str(tmp_path)).make(run_token="run1").delivery()
    assert {k: v.get() for k, v in env.items()} == {
        "TEMP_DIR": f"{tmp_path}/temp/run1",
        "DATA_DIR": str(tmp_path),
        "LOCAL_TEMP_DIR": f"{tmp_path}/temp/run1",
        "LOCAL_DATA_DIR": str(tmp_path),
    }
    assert secrets == {} and env["TEMP_DIR"].source == "storage local"
    scanned, _ = StorageDef("local", str(tmp_path)).make().delivery()
    assert scanned["TEMP_DIR"].get() == f"{tmp_path}/temp/<run_token>"


def test_a_storage_that_declares_no_data_gives_no_data_dir(tmp_path):
    env, _ = StorageDef("local", str(tmp_path)).make(run_token="run1").delivery()
    assert sorted(env) == ["LOCAL_TEMP_DIR", "TEMP_DIR"]


def test_an_s3_storage_takes_its_bucket_and_endpoint_from_an_input():
    s3local = ThingDef("s3local").make()
    s3local.attributes = {
        "bucket": Attribute("ducktest", "s3local"),
        "endpoint": Attribute("http://10.0.0.1:9000", "s3local"),
    }
    store = StorageDef(
        "s3", "s3://{s3local.bucket}", endpoint="{s3local.endpoint}", inputs=["s3local"], data="s3://{s3local.bucket}"
    )
    env, _ = store.make({"s3local": s3local}, run_token="run1").delivery()
    assert {k: v.get() for k, v in env.items()} == {
        "TEMP_DIR": "s3://ducktest/temp/run1",
        "DATA_DIR": "s3://ducktest",
        "S3_ENDPOINT": "10.0.0.1:9000",
        "S3_USE_SSL": "false",
    }


def test_an_azure_storage_delivers_its_account():
    env, _ = StorageDef("dfs", "abfss://c@acct.dfs.core.windows.net").make().delivery()
    assert env["AZ_ACCOUNT"].get() == "acct"
    assert StorageDef("blob", "az://c", account="other").make().delivery()[0]["AZ_ACCOUNT"].get() == "other"


def test_a_reference_to_what_no_input_gives_is_a_usage_error():
    with pytest.raises(UsageError, match=r"\{s3local.bucket\} is not an attribute of an input"):
        StorageDef("s3", "s3://{s3local.bucket}").make()


def test_a_storage_is_shared_rw_and_mints_children_for_an_exclusive_writer():
    store = StorageDef("bucket", "s3://bucket").make(run_token="run1")
    assert store.definition.sharing == SHARED_RW and store.definition.mints_children
    assert store.acquire(RW) is store and store._writer
    store.release(store, RW)
    child = store.acquire(RW, "t1")
    assert child is not store and child.owned


def test_a_writer_with_a_key_gets_a_prefix_of_its_own(tmp_path):
    store = StorageDef("local", str(tmp_path), data=str(tmp_path)).make(run_token="run1")
    parent_identity = store.identity
    one, two = store.acquire(RW, "t1"), store.acquire(RW, "t2")
    assert one.root == f"{tmp_path}/temp/run1/t1" and one.owned and not store.owned
    assert one.identity != two.identity, "two keys gave the same child identity"
    assert store.identity == parent_identity, "minting a child changed the parent's identity"
    env = {k: v.get() for k, v in one.delivery()[0].items()}
    assert env["TEMP_DIR"] == f"{tmp_path}/temp/run1/t1" and env["DATA_DIR"] == str(tmp_path)


def test_a_writer_without_a_key_has_the_whole_root_under_a_lock():
    store = StorageDef("bucket", "s3://bucket").make(run_token="run1")
    assert store.acquire(RW) is store
    assert store._writer


def test_deleting_a_child_removes_what_is_under_it_and_never_the_root(tmp_path):
    store = StorageDef("local", str(tmp_path)).make(run_token="run1")
    store.create()
    child = store.acquire(RW, "t1")
    child.create()
    (tmp_path / "temp" / "run1" / "t1" / "made.parquet").write_text("x")
    child.delete()
    store.delete()
    assert not (tmp_path / "temp" / "run1" / "t1").exists() and tmp_path.exists()


def test_storage_contract(tmp_path):
    check_contract(lambda run_token: StorageDef("local", str(tmp_path)).make(run_token=run_token))


# -----------------------------------------------------------------------------
# A MATRIX value names a storage; its credential comes by scope
#

SECRET = "s3cr3t-value-never-written"
CONF = """
from ducktest5 import default, match
from ducktest5.resources import S3CredentialDef, StorageDef, from_env
STORAGES = [StorageDef("local", "s3://bucket/store")]
CREDENTIALS = [S3CredentialDef("local-key", sources=[from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})], scopes=["local"])]
MATRIX = {"storage": {"local": "local"}}
RULES = [match("a/**", matrix=["storage"])]
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "test" / "a").mkdir(parents=True)
    (tmp_path / "test" / "ducktest_conf.py").write_text(CONF.replace("{root}", str(tmp_path)))
    (tmp_path / "test" / "a" / "x.test").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DT_KEY", "dt-key")
    monkeypatch.setenv("DT_SECRET", SECRET)
    return tmp_path


def test_scan_env_shows_the_storage_roots_and_the_credential_masked(tree, capsys):
    assert main(["scan", "test/a/x.test", "--env"]) == 0
    assert capsys.readouterr().out.replace(str(tree), "ROOT") == textwrap.dedent(
        """\
        a/x.test::x[storage=local]
          bind: storage=local  <- MATRIX storage=local
          require: httpfs  <- credential local-key, storage local
          secret: CREATE SECRET ducktest_local_key (TYPE s3, KEY_ID '{AWS_ACCESS_KEY_ID}', SECRET '{AWS_SECRET_ACCESS_KEY}', SCOPE 's3://bucket/store');  <- credential local-key, storage local
          values: storage=local  <- MATRIX storage
          root: temp_dir_root=s3://bucket/store/temp/<run_token>  <- storage local
          root: data_dir=ROOT/test/data  <- default storage
          process env: AWS_ACCESS_KEY_ID=***  <- credential local-key (S3CredentialDef, from env DT_KEY)
          process env: AWS_SECRET_ACCESS_KEY=***  <- credential local-key (S3CredentialDef, from env DT_SECRET)
        """
    )


def test_scan_env_names_what_the_shells_env_holds_that_engines_do_not_take(tree, capsys, monkeypatch):
    monkeypatch.setattr(credential_module, "_inherit", ())  # put back once the test is done
    conf = tree / "test" / "ducktest_conf.py"
    conf.write_text(conf.read_text() + 'INHERIT_ENV = ["AWS_CA_BUNDLE"]\n')
    for name in ("AWS_PROFILE", "AWS_SECRET_ACCESS_KEY", "AWS_CA_BUNDLE"):
        monkeypatch.setenv(name, "x")
    assert main(["scan", "test/a/x.test", "--env"]) == 0
    out = capsys.readouterr().out
    assert "  process env removed: AWS_PROFILE  <- the shell's env (AWS_ of S3CredentialDef)\n" in out
    assert "  process env kept: AWS_CA_BUNDLE  <- INHERIT_ENV\n" in out
    assert "=x" not in out


def test_the_secret_reaches_the_process_environment_and_no_file_the_run_writes(tree):
    assert (
        main(
            [
                "run",
                "--duckdb-bin",
                FAKE,
                "--unittest-bin",
                FAKE,
                "--temp-dir-root",
                "runs",
                "--keep",
            ]
        )
        == 0
    )
    configs, leaked = [], []
    for dirpath, _, filenames in os.walk(tree / "runs"):
        for name in filenames:
            text = open(os.path.join(dirpath, name), errors="replace").read()
            leaked += [name] if SECRET in text else []
            configs += [json.loads(text)] if name == "config.json" else []
    assert leaked == []
    assert [c["env_passthrough"] for c in configs] == [["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]]
    test = scan(str(tree / "test"), working_dir=str(tree))[0][0]
    assert resolve(test, {}).environ({})["AWS_SECRET_ACCESS_KEY"] == SECRET


def test_a_matrix_value_naming_an_undeclared_input_is_a_usage_error(tree, capsys):
    conf = tree / "test" / "ducktest_conf.py"
    conf.write_text(conf.read_text().replace('{"local": "local"}', '{"local": "nope"}'))
    assert main(["scan", "test/a/x.test", "--env"]) == 2
    assert "MATRIX['storage']['local']: storage='nope' is not in STORAGES" in capsys.readouterr().err


def test_a_credential_that_does_not_resolve_waits_at_scan(tree, monkeypatch, capsys):
    monkeypatch.delenv("DT_SECRET")
    assert main(["scan", "test/a/x.test", "--env"]) == 0
    assert "process env: <credential 'local-key': none of its sources resolved" in capsys.readouterr().out


def test_a_credential_that_does_not_resolve_blocks_its_test_and_the_run_goes_on(tree, monkeypatch, capsys):
    monkeypatch.delenv("DT_SECRET")
    (tree / "test" / "b.test").write_text("")
    args = ["run", "--duckdb-bin", FAKE, "--unittest-bin", FAKE, "--temp-dir-root", "runs"]
    assert main([*args, "--format", "json"]) == 1
    ended = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"test_end"' in line]
    outcomes = {t["id"]: (t["outcome"], t.get("note")) for t in ended}
    assert outcomes["b.test::b"] == ("passed", None)
    outcome, note = outcomes["a/x.test::x[storage=local]"]
    assert outcome == "blocked" and note.startswith("credential 'local-key': none of its sources resolved")


@pytest.mark.parametrize(
    "root, path",
    [("/data/x", "/data/x"), ("file:///data/x", "/data/x"), ("file://localhost/data/x", "/data/x")],
)
def test_a_local_root_is_a_plain_path_or_a_full_file_url(root, path):
    assert local_path(root) == path


def test_a_file_url_with_two_slashes_is_refused():
    with pytest.raises(UsageError, match="file:///path"):
        local_path("file://data/x")
