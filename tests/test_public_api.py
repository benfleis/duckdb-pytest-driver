"""The public API of a conf: the makers on the classes, and the module-level verbs that call them."""

import importlib
import os
import textwrap

import pytest

from ducktest.binary import UsageError
from ducktest.conf import Rule, default, match
from ducktest.engines import spark as spark_module
from ducktest.engines.spark import SparkEngineDef
from ducktest.scan import scan
from ducktest.resources import BootedEngineDef
from ducktest.resources import AzureStorageDef, LocalStorageDef, S3StorageDef, StorageDef
from ducktest.services.azure import AzureServiceDef, container
from ducktest.services.s3 import S3ServiceDef, bucket


# -----------------------------------------------------------------------------
# Rules: the long form on the class, the verbs as convenience
#


def test_default_is_rule_default():
    assert default(tags=["x"], values={"A": "1"}) == Rule.default(tags=["x"], values={"A": "1"})
    assert Rule.default().label == "default"


def test_match_is_rule_match():
    assert match("sql/*.test", requires_env="X", tags=["y"]) == Rule.match("sql/*.test", requires_env="X", tags=["y"])
    assert Rule.match("a.test").label == "match 'a.test'"


# -----------------------------------------------------------------------------
# Services: one class per protocol, the flavor a property, makers on the class
#


def test_rustfs_is_an_s3_service_of_flavor_rustfs():
    local_s3 = S3ServiceDef.rustfs()
    assert (type(local_s3), local_s3.name, local_s3.flavor, local_s3.bucket) == (
        S3ServiceDef,
        "s3",
        "rustfs",
        "ducktest",
    )
    assert local_s3.buckets == [] and local_s3.address is None and local_s3.port is None
    assert S3ServiceDef.rustfs("local_s3").identity() == S3ServiceDef("other", flavor="rustfs").identity()


def test_azurite_is_an_azure_service_of_flavor_azurite():
    local_az = AzureServiceDef.azurite()
    assert (type(local_az), local_az.name, local_az.flavor, local_az.container) == (
        AzureServiceDef,
        "az",
        "azurite",
        "ducktest",
    )


def test_a_service_class_needs_its_name():
    with pytest.raises(TypeError):
        S3ServiceDef()
    with pytest.raises(TypeError):
        AzureServiceDef()


# -----------------------------------------------------------------------------
# Engines
#


def test_spark_delta_is_a_booted_spark_engine():
    spark = SparkEngineDef.delta()
    assert isinstance(spark, BootedEngineDef) and type(spark) is SparkEngineDef
    assert (spark.name, spark.dialect) == ("spark", "spark")
    assert spark.version.startswith("spark:") and spark.version.endswith(":local")


def test_spark_delta_with_s3a_boots_with_the_hadoop_aws_of_its_hadoop(monkeypatch):
    monkeypatch.setattr(spark_module, "_hadoop_version", lambda: "3.4.1")
    booted = []
    monkeypatch.setattr(spark_module, "_boot_delta", lambda *args: booted.append(args) or (None, "4.0"))
    spark = SparkEngineDef.delta(s3a=True)
    assert spark.version.endswith(":local:s3a")
    spark.boot("spark.log")
    assert booted == [("spark.log", ("org.apache.hadoop:hadoop-aws:3.4.1",), True)]


def test_spark_delta_with_s3a_needs_pyspark(monkeypatch):
    monkeypatch.setattr(spark_module, "_hadoop_version", lambda: None)
    with pytest.raises(UsageError, match=r"SparkEngineDef\.delta\('spark', s3a=True\): pyspark is not installed"):
        SparkEngineDef.delta(s3a=True)


def test_spark_from_the_constructor_needs_a_boot():
    with pytest.raises(UsageError, match=r"SparkEngineDef\('spark'\) needs how to boot it; use SparkEngineDef\.delta"):
        SparkEngineDef("spark")


# -----------------------------------------------------------------------------
# Storages: typed by protocol, declared on a service by keyword
#


def test_a_storage_on_a_service_is_typed_by_its_protocol():
    lake = S3ServiceDef.rustfs("local_s3").storage("local_lake", path="lake")
    blobs = AzureServiceDef.azurite("local_az").storage("local_blobs", path="lake")
    assert (type(lake), lake.root) == (S3StorageDef, "s3://{local_s3.bucket}/lake")
    assert (type(blobs), blobs.root) == (AzureStorageDef, "az://{local_az.container}/lake")


def test_storage_takes_only_its_name_by_position():
    local_s3 = S3ServiceDef.rustfs("local_s3", buckets=[bucket("data")])
    local_az = AzureServiceDef.azurite("local_az", containers=[container("data")])
    with pytest.raises(TypeError):
        local_s3.storage("local_lake", "lake")
    with pytest.raises(TypeError):
        local_az.storage("local_blobs", "lake")
    assert local_s3.storage("in_data", bucket="data", path="lake").root == "s3://data/lake"
    assert local_az.storage("in_data", container="data", path="lake").root == "az://data/lake"


def test_a_typed_storage_checks_its_root():
    assert S3StorageDef("lake", "s3://b/p").root == "s3://b/p"
    assert AzureStorageDef("blobs", "abfss://c@a.dfs.core.windows.net/p").root.startswith("abfss://")
    assert LocalStorageDef("scratch", "/tmp/x").root == "/tmp/x"
    with pytest.raises(UsageError, match=r"S3StorageDef\('lake'\): root 'az://c/p' is not s3"):
        S3StorageDef("lake", "az://c/p")
    with pytest.raises(UsageError, match=r"LocalStorageDef\('scratch'\): root 's3://b' is not file"):
        LocalStorageDef("scratch", "s3://b")
    with pytest.raises(UsageError, match="is not az or abfss"):
        AzureStorageDef("blobs", "/tmp/x")
    assert StorageDef("served", "{http.url}").root == "{http.url}"


def test_a_typed_storage_with_a_templated_root_is_checked_when_made():
    on_s3, on_az = StorageDef("on_s3", "s3://b/p"), StorageDef("on_az", "az://c/p")
    lake = S3StorageDef("lake", "{on_s3.root}", inputs=[on_s3])
    assert lake.make({"on_s3": on_s3.make()}).root == "s3://b/p"
    wrong = S3StorageDef("lake", "{on_az.root}", inputs=[on_az])
    with pytest.raises(UsageError, match=r"S3StorageDef\('lake'\): root 'az://c/p' is not s3"):
        wrong.make({"on_az": on_az.make()})


SCOPED_CONF = """
    from ducktest import Project, S3CredentialDef, StorageDef, from_env

    SERVED = StorageDef("served", "s3://b/p")
    project = Project(
        storages=[SERVED, StorageDef("lake", {root!r}, inputs=[SERVED])],
        credentials=[S3CredentialDef("keys", sources=[from_env({{"K": "key_id", "S": "secret"}})], scopes=["lake"])],
    )
"""


@pytest.mark.parametrize("root, refused", [("az://c/p", True), ("{served.root}", False), ("s3://b/q", False)])
def test_a_credential_scope_is_checked_against_a_literal_scheme_only(tmp_path, root, refused):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "ducktest_conf.py").write_text(textwrap.dedent(SCOPED_CONF.format(root=root)))
    if refused:
        with pytest.raises(UsageError, match=r"scoped to storage 'lake' \(az://c/p\): an S3CredentialDef is for s3"):
            scan(str(tmp_path / "test"), working_dir=str(tmp_path))
    else:
        scan(str(tmp_path / "test"), working_dir=str(tmp_path))


# -----------------------------------------------------------------------------
# `path`: one word for a place under something rooted
#


def test_path_names_a_place_under_a_storage_a_bucket_and_temp(tmp_path):
    assert bucket("data", seed=[(str(tmp_path), "dat")]).seed == ((str(tmp_path), "dat"),)
    local = LocalStorageDef("local", str(tmp_path), temp_path="scratch")
    made = local.make(run_token="r1")
    assert made.temp_dir() == os.path.join(str(tmp_path), "scratch", "r1")
    assert local.at("other").path == "other"
    assert local.temp_places() == ["scratch", "other"]


BAD_PATHS = ("/lake", "../x", "a/../b", "a//b", ".", "s3://b/p")


@pytest.mark.parametrize("path", BAD_PATHS)
def test_a_storage_on_a_service_refuses_a_path_that_leaves_or_doubles(path):
    with pytest.raises(UsageError, match=r"service 'local_s3': storage\('l', path=.*\): give a path .* the bucket"):
        S3ServiceDef.rustfs("local_s3").storage("l", path=path)
    with pytest.raises(UsageError, match=r"service 'local_az': storage\('l', path=.*\): give a path .* the container"):
        AzureServiceDef.azurite("local_az").storage("l", path=path)


@pytest.mark.parametrize("path", (*BAD_PATHS, ""))
def test_temp_path_and_at_refuse_a_path_that_leaves_or_doubles(tmp_path, path):
    with pytest.raises(UsageError, match=r"LocalStorageDef\('x', temp_path=.*\): give a path"):
        LocalStorageDef("x", str(tmp_path), temp_path=path)
    with pytest.raises(UsageError, match=r"x\.at\(.*\): give a path"):
        LocalStorageDef("x", str(tmp_path)).at(path)


def test_a_path_may_be_nested_and_a_storage_path_empty():
    local_s3 = S3ServiceDef.rustfs("local_s3")
    assert local_s3.storage("l", path="a/b/").root == "s3://{local_s3.bucket}/a/b"
    assert local_s3.storage("l").root == "s3://{local_s3.bucket}"
    assert AzureServiceDef.azurite("local_az").storage("l", path="a/b").root == "az://{local_az.container}/a/b"
    assert LocalStorageDef("x", "/tmp/x", temp_path="a/b/").temp_path == "a/b"


# -----------------------------------------------------------------------------
# One import line
#

EXPORTED = (
    "Project Rules Rule default match test skip from_env bucket container S3ServiceDef AzureServiceDef "
    "StorageDef S3StorageDef AzureStorageDef LocalStorageDef HttpServerDef S3CredentialDef AzureCredentialDef "
    "DuckDBEngineDef SparkEngineDef default_storage EngineDef ServiceDef Skip L"
).split()
REMOVED = "s3_local az_local duckdb_engine delta_spark s3_credential azure_credential S3LocalDef AZLocalDef".split()


def test_a_conf_imports_everything_from_ducktest():
    import ducktest

    assert sorted(ducktest.__all__) == sorted(EXPORTED)
    namespace = {}
    exec(f"from ducktest import {', '.join(EXPORTED)}", namespace)
    assert all(namespace[name] is getattr(ducktest, name) for name in EXPORTED)


@pytest.mark.parametrize("name", REMOVED)
def test_a_removed_name_is_gone(name):
    with pytest.raises(ImportError):
        exec(f"from ducktest import {name}", {})
    for module in (
        "ducktest.resources",
        "ducktest.services.s3",
        "ducktest.services.azure",
        "ducktest.engines.duckdb",
        "ducktest.engines.spark",
    ):
        with pytest.raises(ImportError):
            exec(f"from {module} import {name}", {})


@pytest.mark.parametrize("module", ["ducktest.services.s3local", "ducktest.services.azlocal"])
def test_a_removed_module_is_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)
