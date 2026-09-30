"""The dialect registry: resource attributes as each engine's preamble."""

from types import SimpleNamespace

import pytest

from ducktest5.binary import UsageError
from ducktest5.resources import CredentialDef, StorageDef, from_env
from ducktest5.resources.dialect import Preamble, pairs, preamble, render

KEYS = {"DT_KEY": "AWS_ACCESS_KEY_ID", "DT_SECRET": "AWS_SECRET_ACCESS_KEY"}


@pytest.fixture
def minio(monkeypatch):
    """A storage on an S3-compatible endpoint and the credential scoped to it, both started."""
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "it's-secret")
    storage = StorageDef("minio", "s3://ducktest/delta", endpoint="http://172.18.0.1:9999").make()
    credential = CredentialDef("minio-key", sources=[from_env(KEYS)], scopes=["minio"]).make()
    credential.start()
    return storage, credential


@pytest.fixture
def two_storages(monkeypatch):
    """One credential scoped to two S3-compatible storages, both started."""
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "it's-secret")
    a = StorageDef("bucket-a", "s3://bucket-a", endpoint="http://172.18.0.1:9999").make()
    b = StorageDef("bucket-b", "s3://bucket-b", endpoint="http://172.18.0.1:9999").make()
    credential = CredentialDef("shared-key", sources=[from_env(KEYS)], scopes=["bucket-a", "bucket-b"]).make()
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
    with pytest.raises(UsageError, match="the dbrx dialect cannot deliver secret"):
        render("dbrx", "secret", None, None)


# -----------------------------------------------------------------------------
# secret
#


def test_duckdb_gets_one_secret_scoped_to_the_storage_root(minio):
    storage, credential = minio
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_minio (TYPE 's3', KEY_ID 'admin', SECRET 'it''s-secret', "
        "ENDPOINT '172.18.0.1:9999', USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/delta');"
    ]


def test_a_credential_scoped_to_two_storages_gets_one_duckdb_secret_with_both_scopes(two_storages):
    assert render("duckdb", "secret", *two_storages).sql == [
        "CREATE SECRET ducktest_bucket_a_bucket_b (TYPE 's3', KEY_ID 'admin', SECRET 'it''s-secret', "
        "ENDPOINT '172.18.0.1:9999', USE_SSL false, URL_STYLE 'path', "
        "SCOPE ['s3://bucket-a', 's3://bucket-b']);"
    ]


def test_spark_gets_the_settings_of_the_one_bucket(minio):
    storage, credential = minio
    assert render("spark", "secret", [storage], credential).conf == {
        "fs.s3a.bucket.ducktest.access.key": "admin",
        "fs.s3a.bucket.ducktest.secret.key": "it's-secret",
        "fs.s3a.bucket.ducktest.aws.credentials.provider": "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
        "fs.s3a.bucket.ducktest.endpoint": "http://172.18.0.1:9999",
        "fs.s3a.bucket.ducktest.path.style.access": "true",
        "fs.s3a.bucket.ducktest.connection.ssl.enabled": "false",
        "fs.s3.impl": "org.apache.hadoop.fs.s3a.S3AFileSystem",
    }


def test_a_credential_scoped_to_two_storages_gets_spark_settings_for_both_buckets(two_storages):
    conf = render("spark", "secret", *two_storages).conf
    assert conf["fs.s3a.bucket.bucket-a.access.key"] == "admin"
    assert conf["fs.s3a.bucket.bucket-b.access.key"] == "admin"
    assert conf["fs.s3a.bucket.bucket-a.secret.key"] == "it's-secret"
    assert conf["fs.s3a.bucket.bucket-b.secret.key"] == "it's-secret"
    assert conf["fs.s3.impl"] == "org.apache.hadoop.fs.s3a.S3AFileSystem"


def test_the_unittest_binary_gets_the_names_of_the_secrets_never_their_values(minio):
    storage, credential = minio
    delivered = render("unittest", "secret", [storage], credential)
    assert delivered == Preamble(
        sql=[
            "CREATE SECRET ducktest_minio (TYPE 's3', KEY_ID '{AWS_ACCESS_KEY_ID}', SECRET '{AWS_SECRET_ACCESS_KEY}', "
            "ENDPOINT '172.18.0.1:9999', USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/delta');"
        ],
        passthrough=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
        requires=["httpfs"],
    )


def test_an_azure_storage_gets_a_service_principal_secret(monkeypatch):
    names = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    for n in names:
        monkeypatch.setenv(n, n.lower())
    storage = StorageDef("az", "abfss://c@acct.dfs.core.windows.net/x").make()
    credential = CredentialDef("spn", sources=[from_env(list(names))], scopes=["az"]).make()
    credential.start()
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_az (TYPE 'azure', PROVIDER 'service_principal', TENANT_ID 'azure_tenant_id', "
        "CLIENT_ID 'azure_client_id', CLIENT_SECRET 'azure_client_secret', ACCOUNT_NAME 'acct', "
        "SCOPE 'abfss://c@acct.dfs.core.windows.net/x');"
    ]


def test_a_local_storage_needs_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    monkeypatch.setenv("DT_SECRET", "s")
    storage = StorageDef("local", str(tmp_path)).make()
    credential = CredentialDef("c", sources=[from_env(KEYS)], scopes=["local"]).make()
    credential.start()
    assert render("duckdb", "secret", [storage], credential) == render("spark", "secret", [storage], credential)
    assert render("duckdb", "secret", [storage], credential) == Preamble()


# -----------------------------------------------------------------------------
# A whole preamble
#


def test_a_credential_pairs_with_the_storage_it_is_scoped_to(minio):
    storage, credential = minio
    other = StorageDef("other", "s3://elsewhere").make()
    assert pairs([credential, other, storage]) == [(credential, [storage])]


def test_a_credential_pairs_with_every_storage_it_is_scoped_to(two_storages):
    storages, credential = two_storages
    assert pairs([*storages, credential]) == [(credential, storages)]


def test_the_preamble_is_requires_then_settings_then_secrets_then_init(minio):
    storage, credential = minio
    session = SimpleNamespace(requires=["httpfs"], settings={"threads": 1}, init=["SELECT 42;"])
    assert preamble("duckdb", session, minio).sql == [
        "LOAD httpfs;",
        "SET threads = '1';",
        render("duckdb", "secret", [storage], credential).sql[0],
        "SELECT 42;",
    ]
