"""ducktest5's MinIO service against a fake docker and a fake boto3 resource."""

import subprocess

import pytest

from ducktest5.services import minio
from ducktest5.resources import ServiceNotUp, StorageDef, check_contract
from ducktest5.resources.dialect import render
from ducktest5.services.minio import MinioDef, minio_settings


class FakeS3:
    """Buckets made. `create_error` is the S3 error code that `create` fails with."""

    def __init__(self, create_error=None):
        self.create_error, self.created = create_error, []

    def Bucket(self, name):  # noqa: N802
        return _Bucket(self, name)


class _Bucket:
    def __init__(self, s3, name):
        self.s3, self.name = s3, name

    def create(self):
        if self.s3.create_error:
            error = Exception(self.s3.create_error)
            error.response = {"Error": {"Code": self.s3.create_error}}
            raise error
        self.s3.created.append(self.name)


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(minio, "_s3", lambda block: fake)
    return fake


class FakeDocker:
    """Containers by name, each running or stopped; a running one answers MinIO's health probe."""

    def __init__(self):
        self.containers = {}

    def __call__(self, *args, check=True):
        verb, name = args[0], args[-1]
        found = name in self.containers
        if verb == "run":
            name = args[args.index("--name") + 1]
            self.containers[name] = True
        elif verb == "stop" and found:
            self.containers[name] = False
        elif verb == "rm":
            self.containers.pop(name, None)
        elif verb == "ps":
            wanted = args[args.index("--filter") + 1].split("=", 1)[1].strip("^$")
            return subprocess.CompletedProcess(args, 0, "\n".join(n for n in self.containers if n == wanted), "")
        return subprocess.CompletedProcess(args, 0 if verb != "inspect" or found else 1, "", "")

    def alive(self, block):
        return self.containers.get(block["container"], False)


@pytest.fixture
def fake_docker(monkeypatch, s3):
    fake = FakeDocker()
    monkeypatch.setattr(minio, "docker", fake)
    monkeypatch.setattr(minio, "minio_alive", fake.alive)
    return fake


def test_minio_keeps_the_contract(fake_docker):
    check_contract(lambda run_token: MinioDef().make(run_token=run_token))


def test_minio_runs_in_a_container_named_for_the_run_and_makes_its_bucket(fake_docker, s3):
    service = MinioDef().make(run_token="r1")
    service.create()
    assert fake_docker.containers == {"ducktest-minio-r1": True}
    assert s3.created == ["ducktest"]
    assert service.list("r1") == ["ducktest-minio-r1"] and service.list("r2") == []
    assert {k: o.get() for k, o in service.outputs().items()} == {
        "minio.endpoint": "http://127.0.0.1:9000",
        "minio.bucket": "ducktest",
        "minio.region": "us-east-1",
        "minio.access_key": "admin",
        "minio.secret_key": "password",
    }


def test_a_failed_run_leaves_the_minio_container_stopped(fake_docker):
    service = MinioDef().make(run_token="r1")
    service.create()
    service.end(failed=True)
    assert fake_docker.containers == {"ducktest-minio-r1": False}
    service.end(failed=False)
    assert fake_docker.containers == {}


def test_minio_produces_a_credential_scoped_to_its_storage_and_emits_no_aws_names(fake_docker):
    definition = MinioDef()
    storage_def = StorageDef("lake", "s3://{minio.bucket}", endpoint="{minio.endpoint}", inputs=[definition])
    credential_def = definition.credential("minio_keys", scopes=["lake"])
    service = definition.make(run_token="r1")
    service.create()
    assert not any(name.startswith("AWS_") for d in service.delivery() for name in d)
    credential = credential_def.make({"minio": service})
    credential.create()
    credential.acquire()
    storage = storage_def.make({"minio": service})
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_lake (TYPE 's3', KEY_ID 'admin', SECRET 'password', REGION 'us-east-1', "
        "ENDPOINT '127.0.0.1:9000', USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest');"
    ]


def test_an_existing_minio_is_adopted_its_bucket_made_and_never_stopped(fake_docker, monkeypatch, s3):
    monkeypatch.setattr(minio, "minio_alive", lambda block: block["endpoint"] == "http://172.18.0.1:9999")
    service = MinioDef().as_existing(address="http://172.18.0.1:9999").make(run_token="r1")
    service.create()
    assert service.env["endpoint"].get() == "http://172.18.0.1:9999" and s3.created == ["ducktest"]
    service.end(failed=False)
    assert fake_docker.containers == {}, "nothing was started, and nothing is removed"


@pytest.mark.parametrize("code", [None, "BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_minio_makes_its_bucket_and_a_bucket_that_is_there_is_fine(fake_docker, monkeypatch, code):
    fake = FakeS3(create_error=code)
    monkeypatch.setattr(minio, "_s3", lambda block: fake)
    MinioDef().make(run_token="r1").create()
    assert fake.created == ([] if code else ["ducktest"])


def test_minio_does_not_hide_another_error_of_its_bucket(fake_docker, monkeypatch):
    monkeypatch.setattr(minio, "_s3", lambda block: FakeS3(create_error="AccessDenied"))
    with pytest.raises(Exception, match="AccessDenied"):
        MinioDef().make(run_token="r1").create()


def test_a_started_minio_is_published_and_reached_on_the_service_host(monkeypatch):
    assert minio_settings(container="c")["endpoint"] == "http://127.0.0.1:9000"
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    settings = minio_settings(container="c")
    assert (settings["endpoint"], settings["publish"]) == ("http://172.18.0.1:9000", "172.18.0.1:9000")


def test_an_existing_minio_that_does_not_answer_is_service_not_up(fake_docker):
    service = MinioDef().as_existing(address="http://172.18.0.1:9").make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="does not answer at http://172.18.0.1:9"):
        service.create()


LIVE_MINIO = "http://172.18.0.1:9999"


@pytest.mark.skipif(not minio.minio_alive({"endpoint": LIVE_MINIO}), reason=f"no MinIO answers at {LIVE_MINIO}")
def test_a_live_existing_minio_keeps_the_contract():
    check_contract(lambda run_token: MinioDef().as_existing(address=LIVE_MINIO).make(run_token=run_token))
