"""The public API of a conf: the makers on the classes, and the module-level verbs that call them."""

import os

import pytest

from ducktest.binary import UsageError
from ducktest.conf import Rule, default, match
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
    assert (type(local_s3), local_s3.name, local_s3.flavor, local_s3.bucket) == (S3ServiceDef, "s3", "rustfs", "ducktest")
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
