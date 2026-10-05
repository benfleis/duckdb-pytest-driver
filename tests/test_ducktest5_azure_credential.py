"""The Azure credential: its providers and what each must give, its secret in both DuckDB dialects, the
schemes it is for, and its delivery as AZURE_* env to the engine processes of a test."""

import json
import os
import re
import textwrap

import pytest

from ducktest5.binary import UsageError
from ducktest5.cli import main
from ducktest5.resources import AzureCredential, AzureCredentialDef, StorageDef, azure_credential, from_env
from ducktest5.resources import credential as credential_module
from ducktest5.resources.dialect import Preamble, makes_secret, render
from ducktest5.scan import scan
from ducktest5.testenv import resolve

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ducktest5_fakes", "fake_unittest.py")
CONNECTION = "DefaultEndpointsProtocol=http;AccountName=acct;AccountKey=a2V5;BlobEndpoint=http://store:10000/acct;"
SPN = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"]


@pytest.fixture
def spn_env(monkeypatch):
    for name in SPN:
        monkeypatch.setenv(name, name.lower() + "-value")


def made(definition):
    credential = definition.make()
    credential.start()
    return credential


# -----------------------------------------------------------------------------
# Providers, and what a source must give
#


def test_an_azure_credential_def_makes_an_azure_credential(monkeypatch):
    monkeypatch.setenv("DT_CONN", CONNECTION)
    credential = made(azure_credential("az", sources=[from_env({"DT_CONN": "connection_string"})]))
    assert isinstance(credential, AzureCredential) and credential.definition.provider == "config"
    assert credential.secrets["AZURE_STORAGE_CONNECTION_STRING"].get() == CONNECTION


@pytest.mark.parametrize(
    "make, message",
    [
        (lambda: azure_credential("a", provider="sas"), "provider 'sas'; it is one of config, service_principal"),
        (lambda: azure_credential("a", chain="env"), "chain is for provider 'credential_chain', not 'config'"),
        (
            lambda: azure_credential("a", sources=[from_env(["AZURE_TENANT_ID"])]),
            "gives no 'connection_string' or 'account_name'; a source must give every required field "
            "(connection_string or account_name)",
        ),
        (
            lambda: azure_credential("a", provider="service_principal", sources=[from_env(SPN[:2])]),
            "gives no 'client_secret' or 'client_certificate_path'",
        ),
        (
            lambda: azure_credential("a", provider="credential_chain", chain="env", sources=[from_env(SPN[:1])]),
            "gives no 'client_id'",
        ),
        (
            lambda: azure_credential(
                "a", sources=[from_env(["AZURE_STORAGE_CONNECTION_STRING", "AZURE_CLIENT_SECRET"])]
            ),
            "provider 'config' takes connection_string, account_name; not client_secret",
        ),
        (lambda: azure_credential("a", sources=[from_env(["AZURE_KEY"])]), "AZURE_KEY is not an env name"),
    ],
)
def test_an_azure_credential_of_the_wrong_shape_is_a_usage_error(make, message):
    with pytest.raises(UsageError, match=re.escape(message)):
        make()


def test_an_account_name_alone_is_a_config_credential_for_anonymous_reads():
    credential = made(azure_credential("public", account_name="acct"))
    assert {n: a.get() for n, a in credential.fields.items()} == {"account_name": "acct"}
    assert {n: a.get() for n, a in credential.secrets.items()} == {"AZURE_STORAGE_ACCOUNT": "acct"}


def test_a_chain_of_env_waits_for_the_service_principal_and_another_chain_needs_nothing(monkeypatch, spn_env):
    names = from_env(SPN)
    assert set(made(azure_credential("a", provider="credential_chain", chain="env", sources=[names])).secrets) == set(
        SPN
    )
    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    with pytest.raises(UsageError, match="none of its sources resolved"):
        made(azure_credential("a", provider="credential_chain", chain="env", sources=[names]))
    assert made(azure_credential("cli", provider="credential_chain", chain="cli")).secrets == {}


def test_from_azure_env_reads_the_names_its_provider_takes(spn_env):
    definition = AzureCredentialDef.from_azure_env("ci", values={"AZURE_AUTH_ENV": "1"})
    assert definition.provider == "credential_chain" and definition.values == {"AZURE_AUTH_ENV": "1"}
    assert [str(s) for s in definition.sources] == [
        "env AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_CLIENT_CERTIFICATE_PATH, AZURE_STORAGE_ACCOUNT"
    ]
    assert set(made(definition).secrets) == set(SPN)


def test_the_provider_and_chain_are_part_of_what_it_is():
    assert len({azure_credential("a", account_name="x").identity(), azure_credential("a").identity()}) == 2
    chains = [azure_credential("a", provider="credential_chain", chain=c).identity() for c in ("env;cli", "cli")]
    assert chains[0] != chains[1]


# -----------------------------------------------------------------------------
# The secret
#


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setenv("DT_CONN", CONNECTION)
    return made(azure_credential("az-keys", sources=[from_env({"DT_CONN": "connection_string"})], scopes=["blobs"]))


def test_duckdb_sets_the_connection_string_from_its_env_and_makes_no_secret(connection):
    storage = StorageDef("blobs", "az://data/lake").make()
    assert render("duckdb", "secret", [storage], connection).sql == [
        "SET azure_storage_connection_string = getenv('AZURE_STORAGE_CONNECTION_STRING');"
    ]
    assert not makes_secret(connection)


def test_the_unittest_binary_gets_the_name_never_the_value_and_requires_azure(connection):
    storage = StorageDef("blobs", "az://data/lake", account="acct").make()
    assert render("unittest", "secret", [storage], connection) == Preamble(
        sql=["SET azure_storage_connection_string = '{AZURE_STORAGE_CONNECTION_STRING}';"],
        requires=["azure"],
    )


def test_an_account_name_alone_is_a_secret_for_anonymous_reads(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "acct")
    credential = made(azure_credential("anon", sources=[from_env(["AZURE_STORAGE_ACCOUNT"])]))
    assert render("duckdb", "secret", [StorageDef("blobs", "az://data/lake").make()], credential).sql == [
        "CREATE SECRET ducktest_anon (TYPE azure, PROVIDER config, ACCOUNT_NAME getenv('AZURE_STORAGE_ACCOUNT'), "
        "SCOPE 'az://data/lake');"
    ]


def test_an_access_token_is_in_the_secret_by_reference(monkeypatch):
    monkeypatch.setenv("AZURE_ACCESS_TOKEN", "a-token")
    credential = made(
        azure_credential("tok", provider="access_token", sources=[from_env(["AZURE_ACCESS_TOKEN"])], account_name="a")
    )
    storage = StorageDef("blobs", "az://data/lake").make()
    sql = [render(d, "secret", [storage], credential).sql[0] for d in ("duckdb", "unittest")]
    assert sql == [
        "CREATE SECRET ducktest_tok (TYPE azure, PROVIDER access_token, ACCESS_TOKEN getenv('AZURE_ACCESS_TOKEN'), "
        "ACCOUNT_NAME getenv('AZURE_STORAGE_ACCOUNT'), SCOPE 'az://data/lake');",
        "CREATE SECRET ducktest_tok (TYPE azure, PROVIDER access_token, ACCESS_TOKEN '{AZURE_ACCESS_TOKEN}', "
        "ACCOUNT_NAME '{AZURE_STORAGE_ACCOUNT}', SCOPE 'az://data/lake');",
    ]
    assert "a-token" not in " ".join(sql)


def test_a_service_principal_secret_names_each_of_its_fields_and_the_account_an_abfss_root_names(spn_env):
    credential = made(azure_credential("spn", provider="service_principal", sources=[from_env(SPN)]))
    storage = StorageDef("lake", "abfss://data@acct.dfs.core.windows.net/lake").make()
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_spn (TYPE azure, PROVIDER service_principal, TENANT_ID getenv('AZURE_TENANT_ID'), "
        "CLIENT_ID getenv('AZURE_CLIENT_ID'), CLIENT_SECRET getenv('AZURE_CLIENT_SECRET'), ACCOUNT_NAME 'acct', "
        "SCOPE 'abfss://data@acct.dfs.core.windows.net/lake');"
    ]


def test_a_chain_secret_holds_no_key_the_sdk_reads_them_from_the_env(spn_env):
    credential = made(
        azure_credential("ci", provider="credential_chain", chain="env", sources=[from_env(SPN)], account_name="acct")
    )
    storages = [StorageDef("a", "az://one").make(), StorageDef("b", "az://two").make()]
    assert render("unittest", "secret", storages, credential).sql == [
        "CREATE SECRET ducktest_ci (TYPE azure, PROVIDER credential_chain, CHAIN 'env', "
        "ACCOUNT_NAME '{AZURE_STORAGE_ACCOUNT}', SCOPE ['az://one', 'az://two']);"
    ]


def test_storages_of_two_accounts_get_no_one_secret(spn_env):
    credential = made(azure_credential("ci", provider="credential_chain"))
    storages = [StorageDef("a", "az://one", account="x").make(), StorageDef("b", "az://two", account="y").make()]
    with pytest.raises(UsageError, match="of different accounts"):
        render("duckdb", "secret", storages, credential)


def test_a_storage_of_another_scheme_is_refused(connection):
    with pytest.raises(UsageError, match=r"an AzureCredentialDef is for az, abfss storages only"):
        render("duckdb", "secret", [StorageDef("blobs", "s3://bucket").make()], connection)


def test_spark_renders_no_azure_secret(connection):
    with pytest.raises(UsageError, match=r"the spark dialect cannot render AzureCredentialDef\('az-keys'\)"):
        render("spark", "secret", [StorageDef("blobs", "az://data").make()], connection)


# -----------------------------------------------------------------------------
# A project: an existing azure .test file, unchanged, holding the credential only
#

CONF = """
from ducktest5 import match
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.resources import AzureCredentialDef, StorageDef, from_env
ENGINES = [duckdb_engine("duck")]
STORAGES = [StorageDef("blobs", "az://data/lake"), StorageDef("tables", "s3://bucket")]
CREDENTIALS = [AzureCredentialDef("az", sources=[from_env({"DT_CONN": "connection_string"})], scopes=["blobs"])]
RULES = [match("existing.test", bind={"credential": "az"}, values={"AZ_TEMP_DIR": "writes"})]
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "ducktest_conf.py").write_text(CONF)
    (tmp_path / "test" / "existing.test").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DT_CONN", CONNECTION)
    return tmp_path


def test_a_test_that_binds_only_the_credential_gets_its_env_and_no_secret(tree, capsys):
    assert main(["scan", "--env"]) == 0
    assert capsys.readouterr().out.replace(str(tree), "ROOT") == textwrap.dedent(
        """\
        existing.test::existing
          client: duck (unittest)  <- ENGINES duck (the only DuckDB engine)
          bind: credential=az  <- ducktest_conf.py: match 'existing.test'
          secret: none (no storage of the test is in its scope)  <- credential az
          values: AZ_TEMP_DIR=writes  <- ducktest_conf.py: match 'existing.test'
          root: temp_dir_root=ROOT/duckdb_unittest_tempdir/<run_token>  <- default storage
          root: data_dir=ROOT/test/data  <- default storage
          process env: AZURE_STORAGE_CONNECTION_STRING=***  <- credential az (AzureCredentialDef, from env DT_CONN)
        """
    )


def test_the_connection_string_reaches_the_binary_by_name_and_no_file_the_run_writes(tree):
    args = [
        "run",
        "--duckdb-bin",
        FAKE,
        "--unittest-bin",
        FAKE,
        "--temp-dir-root",
        "runs",
        "--keep",
    ]
    assert main(args) == 0
    configs, leaked = [], []
    for dirpath, _, filenames in os.walk(tree / "runs"):
        for name in filenames:
            text = open(os.path.join(dirpath, name), errors="replace").read()
            leaked += [name] if "AccountKey=a2V5" in text else []
            configs += [json.loads(text)] if name == "config.json" else []
    assert leaked == []
    assert [c["env_passthrough"] for c in configs] == [["AZURE_STORAGE_CONNECTION_STRING"]]
    test = scan(str(tree / "test"), working_dir=str(tree))[0][0]
    assert resolve(test, {}).environ({})["AZURE_STORAGE_CONNECTION_STRING"] == CONNECTION


def test_a_conf_that_scopes_it_to_an_s3_storage_is_a_scan_error(tree, capsys):
    conf = tree / "test" / "ducktest_conf.py"
    conf.write_text(conf.read_text().replace('scopes=["blobs"]', 'scopes=["tables"]'))
    assert main(["scan"]) == 2
    assert "an AzureCredentialDef is for az, abfss storages only" in capsys.readouterr().err


def test_azure_names_of_the_shells_env_reach_no_engine_but_what_inherit_env_keeps(monkeypatch):
    monkeypatch.setattr(credential_module, "_inherit", ())
    removed = (
        "AZURE_CLIENT_SECRET", "AZURE_FEDERATED_TOKEN_FILE", "AZURE_AUTHORITY_HOST", "IDENTITY_ENDPOINT",
        "MSI_SECRET", "IMDS_ENDPOINT",
    )  # fmt: skip
    for name in (*removed, "AZURE_CONFIG_DIR", "AZ_DATA_DIR"):
        monkeypatch.setenv(name, "x")
    credential_module.inherit_env(["AZURE_CONFIG_DIR"])
    env = credential_module.engine_env()
    assert [n for n in removed if n in env] == []
    assert env["AZURE_CONFIG_DIR"] == env["AZ_DATA_DIR"] == "x"
    with pytest.raises(UsageError, match=r"declare AzureCredentialDef.from_azure_env\(...\) instead"):
        credential_module.inherit_env(["AZURE_CLIENT_SECRET"])
