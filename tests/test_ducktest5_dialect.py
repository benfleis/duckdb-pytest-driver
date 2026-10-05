"""The dialect registry: resource attributes as each engine's preamble."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from ducktest5.binary import UsageError
from ducktest5.engines.spark import env_provider
from ducktest5.resources import CredentialDef, Field, S3CredentialDef, StorageDef, from_env
from ducktest5.resources.dialect import Preamble, pairs, preamble, render

KEYS = {"DT_KEY": "key_id", "DT_SECRET": "secret"}


@pytest.fixture
def remote(monkeypatch):
    """A storage on an S3-compatible endpoint and the credential scoped to it, both started."""
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "it's-secret")
    storage = StorageDef("remote", "s3://ducktest/delta", endpoint="http://172.18.0.1:9999").make()
    credential = S3CredentialDef("remote-key", sources=[from_env(KEYS)], scopes=["remote"]).make()
    credential.start()
    return storage, credential


@pytest.fixture
def two_storages(monkeypatch):
    """One credential scoped to two S3-compatible storages, both started."""
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "it's-secret")
    a = StorageDef("bucket-a", "s3://bucket-a", endpoint="http://172.18.0.1:9999").make()
    b = StorageDef("bucket-b", "s3://bucket-b", endpoint="http://172.18.0.1:9999").make()
    credential = S3CredentialDef("shared-key", sources=[from_env(KEYS)], scopes=["bucket-a", "bucket-b"]).make()
    credential.start()
    return [a, b], credential


# -----------------------------------------------------------------------------
# requires and settings
#


def test_requires_are_load_for_a_shell_and_require_for_the_init_file():
    assert render("duckdb", "requires", ["delta", "httpfs"]).sql == ["LOAD delta;", "LOAD httpfs;"]
    assert render("unittest", "requires", ["delta"]).sql == ["require delta"]


def test_settings_are_set_statements_for_duckdb_and_runtime_conf_for_spark():
    assert render("duckdb", "settings", {"threads": 1, "s": "it's"}).sql == ["SET threads = '1';", "SET s = 'it''s';"]
    assert render("spark", "settings", {"spark.sql.ansi.enabled": True}).conf == {"spark.sql.ansi.enabled": "True"}


def test_a_spark_session_cannot_load_an_extension_at_run_time():
    with pytest.raises(UsageError, match="given when it boots"):
        render("spark", "requires", ["delta"])


def test_an_unknown_dialect_or_action_is_a_usage_error():
    with pytest.raises(UsageError, match="the dbrx dialect cannot deliver settings"):
        render("dbrx", "settings", {})


# -----------------------------------------------------------------------------
# secret
#

CONNECTION = "ENDPOINT '172.18.0.1:9999', URL_STYLE 'path', USE_SSL false, SCOPE 's3://ducktest/delta');"


def test_duckdb_gets_one_secret_scoped_to_the_storage_root_that_reads_the_keys_from_its_env(remote):
    storage, credential = remote
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_remote_key (TYPE s3, KEY_ID getenv('AWS_ACCESS_KEY_ID'), "
        "SECRET getenv('AWS_SECRET_ACCESS_KEY'), " + CONNECTION
    ]


def test_a_storage_with_no_endpoint_gets_the_type_and_the_scope_only(remote):
    _, credential = remote
    storage = StorageDef("aws", "s3://bucket/x").make()
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_remote_key (TYPE s3, KEY_ID getenv('AWS_ACCESS_KEY_ID'), "
        "SECRET getenv('AWS_SECRET_ACCESS_KEY'), SCOPE 's3://bucket/x');"
    ]


def test_a_credential_scoped_to_two_storages_gets_one_duckdb_secret_with_both_scopes(two_storages):
    assert render("duckdb", "secret", *two_storages).sql == [
        "CREATE SECRET ducktest_shared_key (TYPE s3, KEY_ID getenv('AWS_ACCESS_KEY_ID'), "
        "SECRET getenv('AWS_SECRET_ACCESS_KEY'), ENDPOINT '172.18.0.1:9999', URL_STYLE 'path', USE_SSL false, "
        "SCOPE ['s3://bucket-a', 's3://bucket-b']);"
    ]


def test_the_unittest_binary_gets_the_names_of_the_keys_never_their_values_and_requires_httpfs_only(remote):
    storage, credential = remote
    assert render("unittest", "secret", [storage], credential) == Preamble(
        sql=[
            "CREATE SECRET ducktest_remote_key (TYPE s3, KEY_ID '{AWS_ACCESS_KEY_ID}', "
            "SECRET '{AWS_SECRET_ACCESS_KEY}', " + CONNECTION
        ],
        requires=["httpfs"],
    )


def test_a_field_the_source_does_not_give_is_not_in_the_secret(remote, monkeypatch):
    storage, _ = remote
    monkeypatch.setenv("DT_REGION", "eu-west-1")
    keys = from_env({**KEYS, "DT_REGION": "region", "DT_TOKEN": "session_token"})
    monkeypatch.delenv("DT_TOKEN", raising=False)
    credential = S3CredentialDef("remote-key", sources=[keys], scopes=["remote"]).make()
    credential.start()
    (sql,) = render("duckdb", "secret", [storage], credential).sql
    assert "REGION getenv('AWS_REGION')" in sql and "SESSION_TOKEN" not in sql


def test_spark_gets_the_env_provider_and_the_connection_of_the_one_bucket(remote):
    storage, credential = remote
    assert render("spark", "secret", [storage], credential).conf == {
        "fs.s3a.bucket.ducktest.aws.credentials.provider": env_provider(),
        "fs.s3a.bucket.ducktest.endpoint": "http://172.18.0.1:9999",
        "fs.s3a.bucket.ducktest.path.style.access": "true",
        "fs.s3a.bucket.ducktest.connection.ssl.enabled": "false",
        "fs.s3.impl": "org.apache.hadoop.fs.s3a.S3AFileSystem",
    }


def test_a_credential_scoped_to_two_storages_gets_spark_settings_for_both_buckets(two_storages):
    conf = render("spark", "secret", *two_storages).conf
    assert (
        conf["fs.s3a.bucket.bucket-a.endpoint"] == conf["fs.s3a.bucket.bucket-b.endpoint"] == "http://172.18.0.1:9999"
    )
    assert not any("key" in k for k in conf), "keys come from the JVM's env"


@pytest.mark.parametrize(
    "hadoop, provider",
    [
        ("3.3.4", "com.amazonaws.auth.EnvironmentVariableCredentialsProvider"),
        ("3.5.0", "software.amazon.awssdk.auth.credentials.EnvironmentVariableCredentialsProvider"),
    ],
)
def test_the_spark_env_provider_follows_the_hadoop_version(hadoop, provider):
    assert env_provider(hadoop) == provider


def test_a_storage_of_another_scheme_is_refused_where_a_template_gave_it(remote):
    _, credential = remote
    storage = StorageDef("blobs", "az://c/p").make()
    with pytest.raises(UsageError, match=r"S3CredentialDef\('remote-key'\) is scoped to storage 'blobs'"):
        render("duckdb", "secret", [storage], credential)


def test_a_credential_class_a_dialect_does_not_render_is_a_usage_error():
    @dataclass(eq=False)
    class TokenDef(CredentialDef):
        fields = {"token": Field(required=True, secret=True)}
        env = {"token": ("DT_TOKEN",)}

    with pytest.raises(
        UsageError, match=r"the spark dialect cannot render TokenDef\('t'\) \(spark renders: S3CredentialDef\)"
    ):
        render("spark", "secret", [], TokenDef("t").make())


# -----------------------------------------------------------------------------
# A whole preamble
#


def test_a_credential_pairs_with_the_storage_it_is_scoped_to(remote):
    storage, credential = remote
    other = StorageDef("other", "s3://elsewhere").make()
    assert pairs([credential, other, storage]) == [(credential, [storage])]


def test_a_credential_pairs_with_every_storage_it_is_scoped_to(two_storages):
    storages, credential = two_storages
    assert pairs([*storages, credential]) == [(credential, storages)]


def test_the_preamble_is_requires_then_settings_then_secrets_then_init(remote):
    storage, credential = remote
    session = SimpleNamespace(requires=["httpfs"], settings={"threads": 1}, init_sql=["SELECT 42;"])
    assert preamble("duckdb", session, remote).sql == [
        "LOAD httpfs;",
        "SET threads = '1';",
        render("duckdb", "secret", [storage], credential).sql[0],
        "SELECT 42;",
    ]
