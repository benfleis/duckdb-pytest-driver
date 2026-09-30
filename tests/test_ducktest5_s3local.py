"""ducktest5's local S3 server against a fake docker and a fake store."""

import logging
import subprocess

import pytest
from test_ducktest5_leases import opened

from ducktest5 import host
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.resources import RW, ServiceNotUp, check_contract
from ducktest5.resources.dialect import render
from ducktest5.resources.made import FOUND, Made
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
    monkeypatch.setattr(s3local, "has_docker", lambda: True)
    monkeypatch.setattr(s3local, "s3_answers", fake.answers)
    return fake


@pytest.fixture
def no_service_env(monkeypatch):
    for name in ("DUCKTEST_SERVICE_BIND", "DUCKTEST_SERVICE_HOST", "DUCKTEST_S3_PORT"):
        monkeypatch.delenv(name, raising=False)


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


def test_an_engine_bound_to_its_credential_gets_the_keys_in_its_secret(fake_docker):
    definition = s3_local()
    service = definition.make(run_token="r1")
    service.create()
    storage = definition.storage("lake", "lake").make({"s3": service})
    credential = definition.credential("s3_keys", scopes=["lake"]).make({"s3": service})
    engine, made = opened(duckdb_engine("duck"), {"lake": storage, "s3_keys": credential})
    engine.acquire(RW)
    assert "KEY_ID 'admin', SECRET 'password'" in made[0].calls[-1], "the engine's own credential is acquired too"


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


def published_and_reached():
    settings = s3_settings(container="c")
    return settings["publish"], settings["endpoint"]


def test_a_started_server_is_published_and_reached_on_the_loopback_by_default(no_service_env):
    assert published_and_reached() == ("127.0.0.1:9000", "http://127.0.0.1:9000")


def test_the_bind_address_is_where_it_is_published_and_reached(no_service_env, monkeypatch):
    monkeypatch.setenv("DUCKTEST_SERVICE_BIND", "172.18.0.1")
    assert published_and_reached() == ("172.18.0.1:9000", "http://172.18.0.1:9000")
    monkeypatch.setenv("DUCKTEST_SERVICE_BIND", "0.0.0.0")
    assert published_and_reached() == ("0.0.0.0:9000", "http://127.0.0.1:9000")


def test_the_service_host_is_where_it_is_reached_only(no_service_env, monkeypatch):
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    assert published_and_reached() == ("127.0.0.1:9000", "http://172.18.0.1:9000")


def test_a_server_that_already_answers_is_used_as_it_is(no_service_env, monkeypatch, store, caplog):
    def no_docker(*args, **kwargs):
        raise AssertionError(f"docker {args} was called for a server found running")

    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    monkeypatch.setenv("DUCKTEST_S3_PORT", "9999")
    monkeypatch.setattr(s3local, "docker", no_docker)
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: endpoint == "http://172.18.0.1:9999")
    made = Made("r1")
    with caplog.at_level(logging.INFO):
        service = made.create(s3_local())
    assert made.modes["s3"] == FOUND and service.existing and not service.owned
    assert service.attributes["endpoint"].get() == "http://172.18.0.1:9999" and store.buckets == ["ducktest"]
    assert "found running at http://172.18.0.1:9999" in caplog.text
    assert service.read() is not None and service.list("r1") == []
    service.end(failed=True)
    service.end(failed=False)
    service.delete()
    with caplog.at_level(logging.INFO):
        made.end(failed=False)
    assert "service s3 (found running): left alone at http://172.18.0.1:9999" in caplog.text


def test_with_nothing_answering_and_no_docker_it_says_where_it_looked(no_service_env, monkeypatch, store):
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    monkeypatch.setattr(s3local, "has_docker", lambda: False)
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: False)
    monkeypatch.setattr(s3local, "docker", FakeDocker())
    service = s3_local().make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="nothing answers at http://172.18.0.1:9000, and there is no docker here"):
        service.create()
    assert service.list("r1") == [] and service.read() is None


def test_docker_missing_is_a_clear_error_and_a_no_op_for_cleanup(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(host.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="there is no docker here"):
        host.docker("run", "x")
    assert host.docker("rm", "-f", "x", check=False).returncode != 0


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
