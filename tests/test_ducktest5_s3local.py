"""ducktest5's local S3 server against a fake docker and a fake store."""

import subprocess

import pytest

from ducktest5.resources import ServiceNotUp, check_contract
from ducktest5.resources.dialect import render
from ducktest5.services import s3local
from ducktest5.services.s3local import s3_answers, s3_local, s3_settings


class FakeRemote:
    """Buckets made. `create_error` is what making one fails with."""

    def __init__(self, create_error=None):
        self.create_error, self.buckets = create_error, []

    def make_bucket(self, bucket):
        if self.create_error:
            raise RuntimeError(self.create_error)
        self.buckets.append(bucket)


@pytest.fixture
def store(monkeypatch):
    fake = FakeRemote()
    monkeypatch.setattr(s3local, "remote_for", lambda access: fake)
    return fake


class FakeDocker:
    """Containers by name, each running or stopped, and the `docker run` command lines; a running
    one answers at any endpoint."""

    def __init__(self):
        self.containers, self.runs = {}, []

    def __call__(self, *args, check=True):
        verb, name = args[0], args[-1]
        found = name in self.containers
        if verb == "run":
            self.runs.append(args)
            self.containers[args[args.index("--name") + 1]] = True
        elif verb == "stop" and found:
            self.containers[name] = False
        elif verb == "rm":
            self.containers.pop(name, None)
        elif verb == "ps":
            wanted = args[args.index("--filter") + 1].split("=", 1)[1].strip("^$")
            return subprocess.CompletedProcess(args, 0, "\n".join(n for n in self.containers if n == wanted), "")
        return subprocess.CompletedProcess(args, 0 if verb != "inspect" or found else 1, "", "")

    def answers(self, endpoint):
        return any(self.containers.values())


@pytest.fixture
def fake_docker(monkeypatch, store):
    fake = FakeDocker()
    monkeypatch.setattr(s3local, "docker", fake)
    monkeypatch.setattr(s3local, "s3_answers", fake.answers)
    return fake


def test_s3_local_keeps_the_contract(fake_docker):
    check_contract(lambda run_token: s3_local().make(run_token=run_token))


def test_s3_local_runs_in_a_container_named_for_the_run_and_makes_its_bucket(fake_docker, store):
    service = s3_local().make(run_token="r1")
    service.create()
    assert fake_docker.containers == {"ducktest-s3-r1": True}
    assert store.buckets == ["ducktest"]
    assert service.list("r1") == ["ducktest-s3-r1"] and service.list("r2") == []
    assert {k: o.get() for k, o in service.by_ref().items()} == {
        "s3.endpoint": "http://127.0.0.1:9000",
        "s3.bucket": "ducktest",
        "s3.region": "us-east-1",
        "s3.access_key": "admin",
        "s3.secret_key": "password",
    }


def test_the_server_is_started_as_rustfs(fake_docker):
    s3_local().make(run_token="r1").create()
    (run,) = fake_docker.runs
    assert list(run[run.index("-p") + 2 :]) == [
        "-e", "RUSTFS_ACCESS_KEY=admin", "-e", "RUSTFS_SECRET_KEY=password", "-e", "RUSTFS_VOLUMES=/data",
        "-e", "RUSTFS_ADDRESS=:9000", "-e", "RUSTFS_CONSOLE_ENABLE=false", "rustfs/rustfs:1.0.0",
    ]  # fmt: skip


def test_a_failed_run_leaves_the_container_stopped(fake_docker):
    service = s3_local().make(run_token="r1")
    service.create()
    service.end(failed=True)
    assert fake_docker.containers == {"ducktest-s3-r1": False}
    service.end(failed=False)
    assert fake_docker.containers == {}


def test_it_produces_a_credential_scoped_to_its_storage_and_emits_no_aws_names(fake_docker):
    definition = s3_local()
    storage_def = definition.storage("lake", "lake")
    credential_def = definition.credential("s3_keys", scopes=["lake"])
    service = definition.make(run_token="r1")
    service.create()
    assert not any(name.startswith("AWS_") for d in service.delivery() for name in d)
    credential = credential_def.make({"s3": service})
    credential.create()
    credential.acquire()
    storage = storage_def.make({"s3": service})
    assert render("duckdb", "secret", [storage], credential).sql == [
        "CREATE SECRET ducktest_lake (TYPE 's3', KEY_ID 'admin', SECRET 'password', REGION 'us-east-1', "
        "ENDPOINT '127.0.0.1:9000', USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/lake');"
    ]


def test_a_storage_with_no_prefix_is_the_whole_bucket(fake_docker):
    definition = s3_local()
    service = definition.make(run_token="r1")
    service.create()
    assert definition.storage("all").make({"s3": service}).root == "s3://ducktest"


def test_an_existing_server_is_adopted_its_bucket_made_and_never_stopped(fake_docker, monkeypatch, store):
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: endpoint == "http://172.18.0.1:9999")
    service = s3_local().as_existing(address="http://172.18.0.1:9999").make(run_token="r1")
    service.create()
    assert service.attributes["endpoint"].get() == "http://172.18.0.1:9999" and store.buckets == ["ducktest"]
    service.end(failed=False)
    assert fake_docker.containers == {} and fake_docker.runs == [], "nothing was started, and nothing is removed"


def test_an_error_making_its_bucket_is_not_hidden(fake_docker, monkeypatch):
    monkeypatch.setattr(s3local, "remote_for", lambda access: FakeRemote(create_error="AccessDenied"))
    with pytest.raises(RuntimeError, match="AccessDenied"):
        s3_local().make(run_token="r1").create()


def test_a_started_server_is_published_and_reached_on_the_service_host(monkeypatch):
    assert s3_settings(container="c")["endpoint"] == "http://127.0.0.1:9000"
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    settings = s3_settings(container="c")
    assert (settings["endpoint"], settings["publish"]) == ("http://172.18.0.1:9000", "172.18.0.1:9000")


def test_an_existing_server_that_does_not_answer_is_service_not_up(fake_docker):
    service = s3_local().as_existing(address="http://172.18.0.1:9").make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="does not answer at http://172.18.0.1:9"):
        service.create()


def test_a_refused_request_is_an_answer_and_a_server_error_is_not(monkeypatch):
    import urllib.error

    def refused(code):
        def urlopen(url, timeout):
            raise urllib.error.HTTPError(url, code, "", {}, None)

        return urlopen

    monkeypatch.setattr(s3local.urllib.request, "urlopen", refused(403))
    assert s3_answers("http://store:9000")
    monkeypatch.setattr(s3local.urllib.request, "urlopen", refused(503))
    assert not s3_answers("http://store:9000")


LIVE_S3 = "http://172.18.0.1:9999"


@pytest.mark.skipif(not s3_answers(LIVE_S3), reason=f"no S3 server answers at {LIVE_S3}")
def test_a_live_existing_server_keeps_the_contract():
    check_contract(lambda run_token: s3_local().as_existing(address=LIVE_S3).make(run_token=run_token))
