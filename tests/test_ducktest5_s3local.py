"""ducktest5's local S3 server against a fake docker and a fake store."""

import logging
import os
import subprocess

import pytest
from test_ducktest5_leases import opened

from ducktest5 import host
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.resources import RW, ServiceNotUp, check_contract
from ducktest5.resources.dialect import render
from ducktest5.resources.made import FOUND, Made
from ducktest5.services import s3local
from ducktest5.services.s3local import bucket, s3_answers, s3_local, s3_settings


class FakeStore:
    """The server's buckets, as {name: {key: bytes}}, and which are public: its bucket calls (the S3
    API) and the file operations of rclone in one. `created` lists the buckets made, in order;
    `fail_on` names a call that raises."""

    def __init__(self, fail_on=None):
        self.buckets, self.public, self.created, self.calls, self.fail_on = {}, set(), [], [], fail_on

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail_on == name:
            raise RuntimeError(f"{name}: AccessDenied")

    @staticmethod
    def _split(uri):
        bucket, _, key = uri.split("://", 1)[1].partition("/")
        return bucket, f"{key}/" if key else ""

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def create_bucket(self, bucket):
        self._call("create_bucket", bucket)
        if bucket not in self.buckets:
            self.buckets[bucket] = {}
            self.created.append(bucket)

    def delete_bucket(self, bucket, force=False):
        self._call("delete_bucket", bucket)
        assert force or not self.buckets.get(bucket), "a bucket with objects in it is removed with force"
        self.buckets.pop(bucket, None)
        self.public.discard(bucket)

    def make_public(self, bucket):
        self._call("make_public", bucket)
        self.public.add(bucket)

    def is_public(self, bucket):
        return bucket in self.public

    def copy(self, source, uri):
        self._call("copy", source, uri)
        bucket, prefix = self._split(uri)
        self.buckets[bucket].update({prefix + k: v for k, v in files_of(source).items()})

    def check(self, source, uri):
        bucket, prefix = self._split(uri)
        there = self.buckets.get(bucket, {})
        return all(there.get(prefix + k) == v for k, v in files_of(source).items())


def files_of(source):
    out = {}
    for dirpath, _, names in os.walk(source):
        for name in names:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, source)] = f.read()
    return out


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(s3local, "S3Api", lambda *args: fake)
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
    for name in ("DUCKTEST_SERVICE_BIND", "DUCKTEST_SERVICE_HOST", "DUCKTEST_S3_LOCAL_PORT"):
        monkeypatch.delenv(name, raising=False)


def test_s3_local_keeps_the_contract(fake_docker):
    check_contract(lambda run_token: s3_local().make(run_token=run_token))


def test_s3_local_runs_in_a_container_named_for_the_run_and_makes_its_bucket(fake_docker, store):
    service = s3_local().make(run_token="r1")
    service.create()
    assert fake_docker.containers == {"ducktest-s3-r1": True}
    assert store.created == ["ducktest"]
    assert service.list("r1") == ["ducktest-s3-r1"] and service.list("r2") == []
    assert {k: o.get() for k, o in service.by_ref().items()} == {
        "s3.endpoint": "http://127.0.0.1:9000",
        "s3.host_port": "127.0.0.1:9000",
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
        "CREATE SECRET ducktest_s3_keys (TYPE s3, KEY_ID getenv('AWS_ACCESS_KEY_ID'), "
        "SECRET getenv('AWS_SECRET_ACCESS_KEY'), REGION getenv('AWS_REGION'), ENDPOINT '127.0.0.1:9000', "
        "URL_STYLE 'path', USE_SSL false, SCOPE 's3://ducktest/lake');"
    ]


def test_an_engine_bound_to_its_credential_acquires_it(fake_docker):
    definition = s3_local()
    service = definition.make(run_token="r1")
    service.create()
    storage = definition.storage("lake", "lake").make({"s3": service})
    credential = definition.credential("s3_keys", scopes=["lake"]).make({"s3": service})
    engine, made = opened(duckdb_engine("duck"), {"lake": storage, "s3_keys": credential})
    engine.acquire(RW)
    assert "CREATE SECRET ducktest_s3_keys" in made[0].calls[-1]
    assert credential.secrets["AWS_ACCESS_KEY_ID"].get() == "admin", "the engine's own credential is acquired too"


def test_a_storage_with_no_prefix_is_the_whole_bucket(fake_docker):
    definition = s3_local()
    service = definition.make(run_token="r1")
    service.create()
    assert definition.storage("all").make({"s3": service}).root == "s3://ducktest"


def test_an_existing_server_is_adopted_its_bucket_made_and_never_stopped(fake_docker, monkeypatch, store):
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: endpoint == "http://172.18.0.1:9999")
    service = s3_local().as_existing(address="http://172.18.0.1:9999").make(run_token="r1")
    service.create()
    assert service.attributes["endpoint"].get() == "http://172.18.0.1:9999" and store.created == ["ducktest"]
    service.end(failed=False)
    assert fake_docker.containers == {} and fake_docker.runs == [], "nothing was started, and nothing is removed"


def test_an_error_making_its_bucket_is_not_hidden_and_the_server_it_started_goes(fake_docker, store):
    store.fail_on = "create_bucket"
    service = s3_local().make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="create_bucket: AccessDenied"):
        service.create()
    assert fake_docker.containers == {}


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
    monkeypatch.setenv("DUCKTEST_S3_LOCAL_PORT", "9999")
    monkeypatch.setattr(s3local, "docker", no_docker)
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: endpoint == "http://172.18.0.1:9999")
    made = Made("r1")
    with caplog.at_level(logging.INFO):
        service = made.create(s3_local())
    assert made.modes["s3"] == FOUND and service.existing and not service.owned
    assert service.attributes["endpoint"].get() == "http://172.18.0.1:9999" and store.created == ["ducktest"]
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


# -----------------------------------------------------------------------------
# Its buckets
#


@pytest.fixture
def dat(tmp_path):
    """A local directory of two tables."""
    root = tmp_path / "dat"
    for table in ("t1", "t2"):
        (root / table).mkdir(parents=True)
        (root / table / "part-0.parquet").write_bytes(table.encode())
    return str(root)


@pytest.fixture
def found(no_service_env, monkeypatch, store):
    """A server found running: what it holds is the fake store."""
    monkeypatch.setattr(s3local, "docker", FakeDocker())
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: True)
    store.buckets["ducktest"] = {}
    return store


def _holding(store, name, dat, public=False):
    store.buckets[name] = {"dat/" + k: v for k, v in files_of(dat).items()}
    if public:
        store.public.add(name)


def test_buckets_are_made_filled_and_opened_with_the_server_and_removed_at_the_end(found, dat):
    definition = s3_local(
        buckets=[bucket("data", seed=[(dat, "dat")]), bucket("open", seed=[(dat, "dat")], public=True)]
    )
    made = Made("r1")
    service = made.create(definition)
    assert sorted(found.buckets["data"]) == ["dat/t1/part-0.parquet", "dat/t2/part-0.parquet"]
    assert found.public == {"open"} and service.made_buckets == ["data", "open"]
    made.end(failed=False)
    assert sorted(found.buckets) == ["ducktest"], "the server's own bucket stays; the run's go"


def test_a_bucket_that_holds_what_it_seeds_is_used_as_it_is_and_left(found, dat):
    _holding(found, "open", dat, public=True)
    found.buckets["open"]["theirs.csv"] = b"x"
    made = Made("r1")
    service = made.create(s3_local(buckets=[bucket("open", seed=[(dat, "dat")], public=True)]))
    assert service.made_buckets == [] and [c for c in found.calls if c[1] == "open"] == []
    made.end(failed=False)
    assert "theirs.csv" in found.buckets["open"] and [c for c in found.calls if c[1] == "open"] == []


@pytest.mark.parametrize("what", ["content", "public"])
def test_a_bucket_that_is_there_with_other_content_is_refused_and_not_touched(found, dat, what):
    _holding(found, "data", dat)
    if what == "content":
        found.buckets["data"]["dat/t1/part-0.parquet"] = b"changed"
    made = Made("r1")
    with pytest.raises(ServiceNotUp, match="does not change a bucket it did not make"):
        made.create(s3_local(buckets=[bucket("data", seed=[(dat, "dat")], public=what == "public")]))
    assert "s3" in made.errors and [c for c in found.calls if c[1] == "data"] == []


def test_a_bucket_that_fails_half_way_is_removed_with_the_others_it_made(found, dat):
    found.fail_on = "make_public"
    with pytest.raises(ServiceNotUp, match="make_public: AccessDenied"):
        Made("r1").create(s3_local(buckets=[bucket("data", seed=[(dat, "dat")]), bucket("open", public=True)]))
    assert sorted(found.buckets) == ["ducktest"]


def test_a_seed_directory_that_is_not_there_makes_no_bucket(found, tmp_path):
    with pytest.raises(ServiceNotUp, match="no directory at"):
        Made("r1").create(s3_local(buckets=[bucket("data", seed=[(str(tmp_path / "gone"), "dat")])]))
    assert [c for c in found.calls if c[0] != "create_bucket" or c[1] != "ducktest"] == []


@pytest.mark.parametrize("retain, kept", [("never", False), ("on_failure", True), ("always", True)])
def test_retain_keeps_a_made_bucket_after_a_failed_run(found, dat, retain, kept):
    made = Made("r1")
    made.create(s3_local(buckets=[bucket("data", seed=[(dat, "dat")], retain=retain)]))
    made.end(failed=True)
    assert ("data" in found.buckets) is kept


def test_buckets_of_a_server_the_run_started_go_before_it_stops(fake_docker, store, dat):
    service = s3_local(buckets=[bucket("data", seed=[(dat, "dat")])]).make(run_token="r1")
    service.create()
    service.end(failed=False)
    assert "data" not in store.buckets and fake_docker.containers == {}


def test_a_storage_names_one_of_its_buckets():
    definition = s3_local(buckets=[bucket("data")])
    assert definition.storage("d", "lake", bucket="data").root == "s3://data/lake"
    assert definition.storage("own", "lake").root == "s3://{s3.bucket}/lake"
    with pytest.raises(ValueError, match="'nope' is not one of its buckets"):
        definition.storage("x", bucket="nope")


def test_its_buckets_are_part_of_what_it_is(dat):
    assert s3_local().identity() != s3_local(buckets=[bucket("data", seed=[(dat, "dat")])]).identity()


def test_reaching_the_server_after_the_run_makes_none_of_its_buckets(found, dat):
    definition = s3_local(buckets=[bucket("data", seed=[(dat, "dat")])])
    Made("r1").create(definition.reached_at("http://172.18.0.1:9999"))
    assert "data" not in found.buckets
