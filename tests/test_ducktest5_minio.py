"""ducktest5's MinIO service against a fake boto3 resource: what it removes, and what it prints."""

import pytest

from ducktest5.provision import KEPT, Provisioned, attach_argument
from ducktest5.services import minio
from ducktest5.services.minio import minio_block, minio_service


class FakeS3:
    """Keys of one bucket. `create_error` is the S3 error code that `create` fails with."""

    def __init__(self, keys=(), create_error=None):
        self.keys, self.create_error, self.created = set(keys), create_error, []

    def Bucket(self, name):  # noqa: N802
        return _Bucket(self, name)


class _Bucket:
    def __init__(self, s3, name):
        self.s3, self.name, self.objects = s3, name, self

    def create(self):
        if self.s3.create_error:
            error = Exception(self.s3.create_error)
            error.response = {"Error": {"Code": self.s3.create_error}}
            raise error
        self.s3.created.append(self.name)

    def Object(self, key):  # noqa: N802
        return _Deleter(self.s3, lambda k: k == key)

    def filter(self, Prefix):  # noqa: N803
        return _Deleter(self.s3, lambda k: k.startswith(Prefix))


class _Deleter:
    def __init__(self, s3, hit):
        self.s3, self.hit = s3, hit

    def delete(self):
        self.s3.keys -= {k for k in self.s3.keys if self.hit(k)}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3({"temp/run/t/part.parquet", "temp/run/t2/part.parquet", "temp/run/one.csv", "seed/data.parquet"})
    monkeypatch.setattr(minio, "_s3", lambda block: fake)
    return fake


def test_a_location_is_under_the_temp_prefix_of_the_bucket():
    assert minio_service().location(minio_block(), "run", "case", "t") == "s3://ducktest/temp/run/case/t"


def test_clean_removes_a_directory_or_an_object_and_not_a_sibling_with_the_same_start(s3):
    clean, block = minio_service().clean, minio_block()
    clean(block, "s3://ducktest/temp/run/t")
    assert s3.keys == {"temp/run/t2/part.parquet", "temp/run/one.csv", "seed/data.parquet"}
    clean(block, "s3://ducktest/temp/run/one.csv")
    assert s3.keys == {"temp/run/t2/part.parquet", "seed/data.parquet"}


@pytest.mark.parametrize(
    "uri",
    [
        "s3://ducktest/seed",  # in the bucket, and not under the temp prefix
        "s3://ducktest/temp",  # the root itself
        "s3://ducktest/temp/",
        "s3://ducktest/temporary/x",
        "s3://other/temp/run/t",
        "/local/path",
    ],
)
def test_clean_refuses_what_is_not_below_the_temp_prefix(s3, uri):
    before = set(s3.keys)
    with pytest.raises(ValueError, match="only what is there is removed"):
        minio_service().clean(minio_block(), uri)
    assert s3.keys == before


@pytest.mark.parametrize("code", [None, "BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_setup_makes_the_bucket_and_a_bucket_that_is_there_is_fine(monkeypatch, code):
    fake = FakeS3(create_error=code)
    monkeypatch.setattr(minio, "_s3", lambda block: fake)
    minio_service().setup(minio_block())
    assert fake.created == ([] if code else ["ducktest"])


def test_setup_does_not_hide_another_error(monkeypatch):
    monkeypatch.setattr(minio, "_s3", lambda block: FakeS3(create_error="AccessDenied"))
    with pytest.raises(Exception, match="AccessDenied"):
        minio_service().setup(minio_block())


def test_the_argument_for_the_next_run_holds_what_differs_from_the_defaults_and_no_secret(monkeypatch):
    service = minio_service()
    assert attach_argument(Provisioned(service, minio_block(), KEPT)) == (
        """--existing-service 'minio={"endpoint": "http://127.0.0.1:9000"}'"""
    )
    monkeypatch.setenv("DUCKTEST_MINIO_BUCKET", "mine")
    monkeypatch.setenv("DUCKTEST_MINIO_REGION", "eu-west-1")
    monkeypatch.setenv("DUCKTEST_MINIO_SECRET_KEY", "hunter2")
    monkeypatch.setenv("DUCKTEST_MINIO_S3_PORT", "9100")
    printed = attach_argument(Provisioned(service, minio_block(), KEPT))
    assert printed.startswith(
        """--existing-service 'minio={"endpoint": "http://127.0.0.1:9100", "region": "eu-west-1", """
        """"secret_key": "***", "bucket": "mine", "port": 9100}'"""
    )
    assert "hunter2" not in printed and "set it in the environment" in printed
    assert minio_block()["bucket"] == "mine"  # the environment is as it was
