"""Resources: identity by content, ownership against sharing, outputs, and the handler contract."""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time

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
    Output,
    Resource,
    StorageDef,
    CredentialDef,
    check_contract,
    from_env,
    pick_credentials,
    local_path,
    scheme_of,
)
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


def test_an_expired_instance_is_renewed_at_acquire():
    thing = ThingDef("a").make()
    renewed = []
    thing.expires_at, thing.renew = (lambda: time.time() - 1), (lambda: renewed.append(1))
    thing.acquire(RO)
    assert renewed == [1]


def test_retain_says_when_the_run_leaves_it_in_place():
    assert ThingDef("a").make().retained(failed=True)
    assert not ThingDef("a").make().retained(failed=False)
    assert not ThingDef("a", retain=NEVER).make().retained(failed=True)


# -----------------------------------------------------------------------------
# Outputs
#


def test_outputs_are_named_under_the_producer_and_delivered_as_the_kind_names_them():
    thing = ThingDef("a").make()
    thing.env["ENDPOINT"] = Output("http://x", "thing a")
    thing.secrets["KEY"] = Output(lambda: "k", "thing a")
    assert set(thing.outputs()) == {"a.ENDPOINT", "a.KEY"}
    env, secrets = thing.delivery()
    assert env["ENDPOINT"].get() == "http://x" and secrets["KEY"].get() == "k"


def test_an_output_of_none_unsets_the_name():
    assert Output(None, "x").unset and not Output("", "x").unset


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


def test_a_credential_takes_the_first_source_that_resolves(monkeypatch):
    monkeypatch.delenv("DT_MISSING", raising=False)
    monkeypatch.setenv("DT_KEY", "k1")
    cred = CredentialDef("key", sources=[from_env(["DT_MISSING"]), from_env({"DT_KEY": "AWS_ACCESS_KEY_ID"})]).make()
    cred.create()
    assert cred.env == {}
    assert cred.secrets["AWS_ACCESS_KEY_ID"].source == "env DT_KEY"
    monkeypatch.setenv("DT_KEY", "k2")  # read when delivered, not when resolved
    assert cred.delivery()[1]["AWS_ACCESS_KEY_ID"].get() == "k2"


def test_a_credential_none_of_whose_sources_resolve_says_which_it_tried(monkeypatch):
    monkeypatch.delenv("DT_MISSING", raising=False)
    with pytest.raises(UsageError, match=r"'key': none of its sources resolved: \{'env': \{'DT_MISSING'"):
        CredentialDef("key", sources=[from_env(["DT_MISSING"])]).make().create()


def test_1password_is_not_available_here(monkeypatch):
    monkeypatch.delenv("DT_MISSING", raising=False)
    cred = CredentialDef("key", sources=[from_env(["DT_MISSING"]), "1password:ducktest-aws"]).make()
    with pytest.raises(UsageError, match=r"'key': source 1password:ducktest-aws is not available here"):
        cred.create()


def test_a_credential_counts_by_its_source_never_its_value(monkeypatch):
    d = CredentialDef("key", sources=[from_env(["DT_KEY"])])
    monkeypatch.setenv("DT_KEY", "one")
    before = d.identity()
    monkeypatch.setenv("DT_KEY", "two")
    assert d.identity() == before
    assert CredentialDef("key", sources=[from_env(["DT_OTHER"])]).identity() != before


def test_two_env_sourced_credentials_differ_by_the_names_they_read():
    a = CredentialDef("key", sources=[from_env(["DT_A"])]).identity()
    b = CredentialDef("key", sources=[from_env(["DT_B"])]).identity()
    assert a != b


def test_a_callable_source_makes_the_secret_from_the_inputs_at_acquire(monkeypatch):
    monkeypatch.setenv("DT_KEY", "root")
    calls = []

    def token(inputs):
        calls.append(1)
        return {"UC_TOKEN": "t-" + inputs["root"].secrets["KEY"].get()}

    root = CredentialDef("root", sources=[from_env({"DT_KEY": "KEY"})]).make()
    root.create()
    cred = CredentialDef("token", sources=[token], inputs=["root"]).make({"root": root})
    cred.create()
    assert calls == [] and cred.secrets == {}
    cred.acquire()
    assert cred.secrets["UC_TOKEN"].get() == "t-root"
    assert cred.secrets["UC_TOKEN"].source.startswith("callable ")


def test_a_callable_source_returning_none_falls_through_to_the_next_source(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    calls = []

    def skip(inputs):
        calls.append(1)
        return None

    cred = CredentialDef("key", sources=[skip, from_env(["DT_KEY"])]).make()
    cred.create()
    cred.acquire()
    assert calls == [1]
    assert cred.secrets["DT_KEY"].source == "env DT_KEY"


def test_env_colon_name_is_shorthand_for_env_of_one_name(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    cred = CredentialDef("key", sources=["env:DT_KEY"]).make()
    cred.create()
    assert cred.secrets["DT_KEY"].source == "env DT_KEY"


def test_a_plain_string_source_that_is_not_a_reference_is_a_usage_error():
    with pytest.raises(UsageError, match="names where a secret lives"):
        CredentialDef("key", sources=["oops"])


def test_a_credentials_default_retain_is_never():
    assert CredentialDef("key").retain == NEVER


S3_KEY = CredentialDef("s3-key", scopes=["s3store"])
S3_OTHER = CredentialDef("s3-other", scopes=["s3store"])
S3_READ = CredentialDef("s3-read", scopes=[("s3store", RO)])
AZ_KEY = CredentialDef("az-key", scopes=["azstore"])


def test_a_storage_gets_the_credential_scoped_to_it():
    assert pick_credentials({"s3store": RW, "azstore": RW}, [S3_KEY, AZ_KEY]) == [S3_KEY, AZ_KEY]


def test_a_read_only_credential_serves_a_read_only_edge_only():
    assert pick_credentials({"s3store": RW}, [S3_READ]) == []
    assert pick_credentials({"s3store": RO}, [S3_READ]) == [S3_READ]


def test_two_credentials_for_one_storage_none_named_is_a_usage_error_listing_them():
    with pytest.raises(UsageError, match=r"storage 's3store' has credentials \['s3-key', 's3-other'\]"):
        pick_credentials({"s3store": RW}, [S3_KEY, S3_OTHER])


def test_the_default_credential_breaks_the_tie():
    default = CredentialDef("s3-default", scopes=["s3store"], default=True)
    assert pick_credentials({"s3store": RW}, [S3_KEY, S3_OTHER, default]) == [default]


def test_a_named_credential_wins_over_scope_so_a_wrong_credential_test_can_be_declared():
    wrong = CredentialDef("wrong", scopes=["s3store"])
    none = CredentialDef("none")
    assert pick_credentials({"s3store": RW}, [S3_KEY, S3_OTHER], named=[wrong]) == [wrong]
    assert pick_credentials({"s3store": RW}, [S3_KEY], named=[none]) == [none, S3_KEY]


def test_a_credential_scoped_to_two_storages_serves_both():
    both = CredentialDef("both", scopes=["s3store", "azstore"])
    assert pick_credentials({"s3store": RW, "azstore": RW}, [both]) == [both]


def test_a_storage_matched_by_two_credentials_still_errors_unless_one_is_named_or_default():
    both = CredentialDef("both", scopes=["s3store", "azstore"])
    with pytest.raises(UsageError, match=r"storage 's3store' has credentials \['both', 's3-key'\]"):
        pick_credentials({"s3store": RW, "azstore": RW}, [both, S3_KEY])


def test_a_credential_scoped_to_storage_a_is_not_delivered_for_a_test_that_uses_only_storage_b():
    assert pick_credentials({"azstore": RW}, [S3_KEY]) == []


def test_credential_contract(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    check_contract(lambda run_token: CredentialDef("key", sources=[from_env(["DT_KEY"])]).make(run_token=run_token))


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
    env, secrets = StorageDef("local", str(tmp_path)).make().delivery()
    assert {k: v.get() for k, v in env.items()} == {
        "TEMP_DIR": f"{tmp_path}/temp",
        "DATA_DIR": str(tmp_path),
        "LOCAL_TEMP_DIR": f"{tmp_path}/temp",
        "LOCAL_DATA_DIR": str(tmp_path),
    }
    assert secrets == {} and env["TEMP_DIR"].source == "storage local"


def test_an_s3_storage_takes_its_bucket_and_endpoint_from_an_input():
    minio = ThingDef("minio").make()
    minio.env = {"bucket": Output("ducktest", "minio"), "endpoint": Output("http://10.0.0.1:9000", "minio")}
    store = StorageDef("s3", "s3://{minio.bucket}", endpoint="{minio.endpoint}", inputs=["minio"])
    env, _ = store.make({"minio": minio}).delivery()
    assert {k: v.get() for k, v in env.items()} == {
        "TEMP_DIR": "s3://ducktest/temp",
        "DATA_DIR": "s3://ducktest",
        "S3_ENDPOINT": "10.0.0.1:9000",
        "S3_USE_SSL": "false",
    }


def test_an_azure_storage_delivers_its_account():
    env, _ = StorageDef("dfs", "abfss://c@acct.dfs.core.windows.net").make().delivery()
    assert env["AZ_ACCOUNT"].get() == "acct"
    assert StorageDef("blob", "az://c", account="other").make().delivery()[0]["AZ_ACCOUNT"].get() == "other"


def test_a_reference_to_what_no_input_gives_is_a_usage_error():
    with pytest.raises(UsageError, match=r"\{minio.bucket\} is not an output of an input"):
        StorageDef("s3", "s3://{minio.bucket}").make()


def test_a_storage_is_shared_rw_and_mints_children_for_an_exclusive_writer():
    store = StorageDef("bucket", "s3://bucket").make(run_token="run1")
    assert store.definition.sharing == SHARED_RW and store.definition.mints_children
    assert store.acquire(RW) is store and store._writer
    store.release(store, RW)
    child = store.acquire(RW, "t1")
    assert child is not store and child.owned


def test_a_writer_with_a_key_gets_a_prefix_of_its_own(tmp_path):
    store = StorageDef("local", str(tmp_path)).make(run_token="run1")
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
# A variant names a storage; its credential comes by scope
#

SECRET = "s3cr3t-value-never-written"
CONF = """
from ducktest5 import Variant, default, match
from ducktest5.resources import CredentialDef, StorageDef, from_env
STORAGES = [StorageDef("local", "{root}/store")]
CREDENTIALS = [CredentialDef("local-key", sources=[from_env({"DT_SECRET": "AWS_SECRET_ACCESS_KEY"})], scopes=["local"])]
AXES = {"storage": [Variant("local", inputs=["local"])]}
RULES = [match("a/**", variants=["storage"])]
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "test" / "a").mkdir(parents=True)
    (tmp_path / "test" / "ducktest_conf.py").write_text(CONF.replace("{root}", str(tmp_path)))
    (tmp_path / "test" / "a" / "x.test").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DT_SECRET", SECRET)
    return tmp_path


def test_scan_env_shows_the_storage_roots_and_the_credential_masked(tree, capsys):
    assert main(["scan", "test/a/x.test", "--env"]) == 0
    assert capsys.readouterr().out.replace(str(tree), "ROOT") == textwrap.dedent(
        """\
        a/x.test::x[storage=local]
          values: storage=local  <- AXES storage
          root: temp_dir_root=ROOT/store/temp  <- storage local
          root: data_dir=ROOT/store  <- storage local
          env: AWS_SECRET_ACCESS_KEY=***  <- credential local-key: env DT_SECRET
        """
    )


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
                "--no-binary-check",
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
    assert [c["env_passthrough"] for c in configs] == [["AWS_SECRET_ACCESS_KEY"]]
    test = scan(str(tree / "test"), working_dir=str(tree))[0][0]
    assert resolve(test, {}).environ({})["AWS_SECRET_ACCESS_KEY"] == SECRET


def test_a_variant_naming_an_undeclared_input_is_a_usage_error(tree, capsys):
    conf = tree / "test" / "ducktest_conf.py"
    conf.write_text(conf.read_text().replace('inputs=["local"]', 'inputs=["nope"]'))
    assert main(["scan", "test/a/x.test", "--env"]) == 2
    assert "names input 'nope', which is not in STORAGES or CREDENTIALS" in capsys.readouterr().err


def test_a_credential_that_does_not_resolve_waits_at_scan(tree, monkeypatch, capsys):
    monkeypatch.delenv("DT_SECRET")
    assert main(["scan", "test/a/x.test", "--env"]) == 0
    assert "env: <credential 'local-key': none of its sources resolved" in capsys.readouterr().out


@pytest.mark.parametrize(
    "root, path",
    [("/data/x", "/data/x"), ("file:///data/x", "/data/x"), ("file://localhost/data/x", "/data/x")],
)
def test_a_local_root_is_a_plain_path_or_a_full_file_url(root, path):
    assert local_path(root) == path


def test_a_file_url_with_two_slashes_is_refused():
    with pytest.raises(UsageError, match="file:///path"):
        local_path("file://data/x")
