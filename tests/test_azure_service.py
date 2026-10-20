"""ducktest's local Azure blob server against a fake docker and a fake store."""

import logging
import os

import pytest
from test_leases import opened
from test_s3_service import FakeDocker, files_of

from ducktest.binary import UsageError
from ducktest.engines.duckdb import DuckEngineDef
from ducktest.resources import RW, ServiceNotUp, StorageDef, check_contract
from ducktest.resources.dialect import render
from ducktest.resources.made import FOUND, Made
from ducktest.services import azure as azure_module
from ducktest.services.azure import AZURITE_KEY, az_answers, AzureServiceDef, az_settings, container

CONNECTION = (
    f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={AZURITE_KEY};"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


class FakeStore:
    """The server's containers, as {name: {key: bytes}}, and their public levels: its container calls
    (the Blob service) and the file operations of rclone in one. `created` lists the containers made,
    in order; `fail_on` names a call that raises."""

    def __init__(self, fail_on=None):
        self.containers, self.public, self.created, self.calls, self.fail_on = {}, {}, [], [], fail_on

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail_on == name:
            raise RuntimeError(f"{name}: AuthorizationFailure")

    @staticmethod
    def _split(uri):
        name, _, key = uri.split("://", 1)[1].partition("/")
        return name, f"{key}/" if key else ""

    def container_exists(self, name):
        return name in self.containers

    def public_access(self, name):
        return self.public.get(name)

    def create_container(self, name):
        self._call("create_container", name)
        if name not in self.containers:
            self.containers[name] = {}
            self.created.append(name)

    def delete_container(self, name):
        self._call("delete_container", name)
        self.containers.pop(name, None)
        self.public.pop(name, None)

    def set_public_access(self, name, level):
        self._call("set_public_access", name, level)
        self.public[name] = level

    def copy(self, source, uri):
        self._call("copy", source, uri)
        name, prefix = self._split(uri)
        self.containers[name].update({prefix + k: v for k, v in files_of(source).items()})

    def check(self, source, uri):
        name, prefix = self._split(uri)
        there = self.containers.get(name, {})
        return all(there.get(prefix + k) == v for k, v in files_of(source).items())


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(azure_module, "BlobApi", lambda *args: fake)
    monkeypatch.setattr(azure_module, "remote_for", lambda access: fake)
    return fake


@pytest.fixture
def fake_docker(monkeypatch, store):
    fake = FakeDocker()
    monkeypatch.setattr(azure_module, "docker", fake)
    monkeypatch.setattr(azure_module, "has_docker", lambda: True)
    monkeypatch.setattr(azure_module, "az_answers", fake.answers)
    return fake


@pytest.fixture
def no_service_env(monkeypatch):
    for name in ("DUCKTEST_SERVICE_BIND", "DUCKTEST_SERVICE_HOST", "DUCKTEST_AZ_LOCAL_PORT"):
        monkeypatch.delenv(name, raising=False)


def test_azurite_keeps_the_contract(fake_docker):
    check_contract(lambda run_token: AzureServiceDef.azurite().make(run_token=run_token))


def test_azurite_runs_in_a_container_named_for_the_run_and_makes_its_container(fake_docker, store):
    service = AzureServiceDef.azurite().make(run_token="r1")
    service.create()
    assert fake_docker.containers == {"ducktest-az-r1": True}
    assert store.created == ["ducktest"]
    assert service.list("r1") == ["ducktest-az-r1"] and service.list("r2") == []
    assert {k: o.get() for k, o in service.by_ref().items()} == {
        "az.endpoint": "http://127.0.0.1:10000/devstoreaccount1",
        "az.account": "devstoreaccount1",
        "az.container": "ducktest",
        "az.key": AZURITE_KEY,
        "az.connection_string": CONNECTION,
    }


def test_the_server_is_started_as_azurite_blob_only(fake_docker):
    AzureServiceDef.azurite().make(run_token="r1").create()
    (run,) = fake_docker.runs
    assert list(run[run.index("-p") :]) == [
        "-p", "127.0.0.1:10000:10000", "mcr.microsoft.com/azure-storage/azurite:3.37.0",
        "azurite-blob", "--blobHost", "0.0.0.0", "--blobPort", "10000", "--location", "/data",
        "--skipApiVersionCheck",
    ]  # fmt: skip


def test_an_account_of_its_own_is_given_to_azurite(fake_docker, monkeypatch):
    monkeypatch.setenv("DUCKTEST_AZ_LOCAL_ACCOUNT", "mine")
    monkeypatch.setenv("DUCKTEST_AZ_LOCAL_KEY", "a2V5")
    service = AzureServiceDef.azurite().make(run_token="r1")
    service.create()
    (run,) = fake_docker.runs
    assert run[run.index("-e") + 1] == "AZURITE_ACCOUNTS=mine:a2V5"
    assert service.attributes["endpoint"].get() == "http://127.0.0.1:10000/mine"


def test_the_flavor_names_what_backs_it():
    assert AzureServiceDef.azurite().flavor == "azurite"
    with pytest.raises(UsageError, match="flavor 'minio'; it is one of azurite"):
        AzureServiceDef("az", flavor="minio")


def test_a_failed_run_leaves_the_container_stopped(fake_docker):
    service = AzureServiceDef.azurite().make(run_token="r1")
    service.create()
    service.end(failed=True)
    assert fake_docker.containers == {"ducktest-az-r1": False}
    service.end(failed=False)
    assert fake_docker.containers == {}


def test_it_produces_a_credential_scoped_to_its_storage_and_emits_no_azure_names(fake_docker):
    definition = AzureServiceDef.azurite()
    storage_def = definition.storage("blobs", path="lake")
    credential_def = definition.credential("az_keys", scopes=["blobs"])
    service = definition.make(run_token="r1")
    service.create()
    assert not any(name.startswith("AZURE_") for d in service.delivery() for name in d)
    credential = credential_def.make({"az": service})
    credential.create()
    credential.acquire()
    assert {n: a.get() for n, a in credential.secrets.items()} == {
        "AZURE_STORAGE_CONNECTION_STRING": CONNECTION,
        "AZURE_STORAGE_ACCOUNT": "devstoreaccount1",
    }
    storage = storage_def.make({"az": service})
    assert storage.root == "az://ducktest/lake"
    assert {k: v.get() for k, v in storage.delivery()[0].items()}["AZ_ACCOUNT"] == "devstoreaccount1"
    (sql,) = render("duck-shell", "secret", [storage], credential).sql
    assert sql.startswith("CREATE SECRET ducktest_") and sql.endswith("SCOPE 'az://ducktest/lake');")
    assert "PROVIDER config, CONNECTION_STRING getenv('AZURE_STORAGE_CONNECTION_STRING')" in sql


def test_an_engine_bound_to_its_credential_acquires_it(fake_docker):
    definition = AzureServiceDef.azurite()
    service = definition.make(run_token="r1")
    service.create()
    storage = definition.storage("blobs", path="lake").make({"az": service})
    credential = definition.credential("az_keys", scopes=["blobs"]).make({"az": service})
    engine, made = opened(DuckEngineDef("duck"), {"blobs": storage, "az_keys": credential})
    engine.acquire(RW)
    assert "CREATE SECRET ducktest_az_keys (TYPE azure, PROVIDER config, CONNECTION_STRING getenv(" in made[0].calls[-1]
    assert credential.secrets["AZURE_STORAGE_CONNECTION_STRING"].get() == CONNECTION


def test_a_storage_names_one_of_its_containers_and_is_az_only():
    definition = AzureServiceDef.azurite(containers=[container("data")])
    assert definition.storage("d", path="lake", container="data").root == "az://data/lake"
    assert definition.storage("own").root == "az://{az.container}"
    with pytest.raises(UsageError, match="'nope' is not one of its containers"):
        definition.storage("x", container="nope")
    with pytest.raises(UsageError, match="it has no DFS endpoint for abfss://"):
        definition.check_storage(
            StorageDef("dfs", "abfss://data@devstoreaccount1.dfs.core.windows.net", inputs=[definition])
        )


def test_an_abfss_storage_on_it_is_a_scan_error(tmp_path, monkeypatch, capsys):
    from ducktest.cli import main

    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "x.test").write_text("")
    (tmp_path / "test" / "ducktest_conf.py").write_text(
        "from ducktest import AzureServiceDef, Project, StorageDef\n"
        "AZ = AzureServiceDef.azurite()\n"
        'project = Project(storages=[StorageDef("dfs", "abfss://ducktest@devstoreaccount1.dfs.core.windows.net", inputs=[AZ])])\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["scan"]) == 2
    assert "is on service 'az' (azurite), which serves blob storage only" in capsys.readouterr().err


def published_and_reached():
    settings = az_settings(docker_name="c")
    return settings["publish"], settings["endpoint"]


def test_a_started_server_is_published_and_reached_on_the_loopback_by_default(no_service_env):
    assert published_and_reached() == ("127.0.0.1:10000", "http://127.0.0.1:10000/devstoreaccount1")


def test_the_bind_address_and_the_service_host(no_service_env, monkeypatch):
    monkeypatch.setenv("DUCKTEST_SERVICE_BIND", "0.0.0.0")
    assert published_and_reached() == ("0.0.0.0:10000", "http://127.0.0.1:10000/devstoreaccount1")
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    monkeypatch.setenv("DUCKTEST_AZ_LOCAL_PORT", "10010")
    assert published_and_reached() == ("0.0.0.0:10010", "http://172.18.0.1:10010/devstoreaccount1")


def test_a_server_that_already_answers_is_used_as_it_is(no_service_env, monkeypatch, store, caplog):
    def no_docker(*args, **kwargs):
        raise AssertionError(f"docker {args} was called for a server found running")

    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    monkeypatch.setattr(azure_module, "docker", no_docker)
    monkeypatch.setattr(
        azure_module, "az_answers", lambda endpoint: endpoint == "http://172.18.0.1:10000/devstoreaccount1"
    )
    made = Made("r1")
    with caplog.at_level(logging.INFO):
        service = made.create(AzureServiceDef.azurite())
    assert made.modes["az"] == FOUND and service.existing and not service.owned
    assert store.created == ["ducktest"] and "found running at http://172.18.0.1:10000" in caplog.text
    service.end(failed=False)
    service.delete()
    with caplog.at_level(logging.INFO):
        made.end(failed=False)
    assert "service az (found running): left alone at http://172.18.0.1:10000" in caplog.text


def test_an_existing_server_is_named_by_its_address_without_the_account(fake_docker, monkeypatch, store):
    monkeypatch.setattr(
        azure_module, "az_answers", lambda endpoint: endpoint == "http://172.18.0.1:10999/devstoreaccount1"
    )
    service = AzureServiceDef.azurite().as_existing(address="http://172.18.0.1:10999").make(run_token="r1")
    service.create()
    assert service.attributes["endpoint"].get() == "http://172.18.0.1:10999/devstoreaccount1"
    service.end(failed=False)
    assert fake_docker.runs == []


def test_with_nothing_answering_and_no_docker_it_says_where_it_looked(no_service_env, monkeypatch, store):
    monkeypatch.setattr(azure_module, "has_docker", lambda: False)
    monkeypatch.setattr(azure_module, "az_answers", lambda endpoint: False)
    monkeypatch.setattr(azure_module, "docker", FakeDocker())
    with pytest.raises(ServiceNotUp, match="nothing answers at http://127.0.0.1:10000, and there is no docker here"):
        AzureServiceDef.azurite().make(run_token="r1").create()


def test_a_refused_listing_is_an_answer_and_a_server_error_is_not(monkeypatch):
    import urllib.error

    def refused(code):
        def urlopen(url, timeout):
            assert url == "http://store:10000/acct?comp=list"
            raise urllib.error.HTTPError(url, code, "", {}, None)

        return urlopen

    monkeypatch.setattr(azure_module.urllib.request, "urlopen", refused(403))
    assert az_answers("http://store:10000/acct")
    monkeypatch.setattr(azure_module.urllib.request, "urlopen", refused(503))
    assert not az_answers("http://store:10000/acct")


HOST = os.environ.get("DUCKTEST_SERVICE_HOST", "127.0.0.1")
LIVE_AZ = f"http://{HOST}:{os.environ.get('DUCKTEST_AZ_LOCAL_PORT', '10000')}"


@pytest.mark.skipif(not az_answers(f"{LIVE_AZ}/devstoreaccount1"), reason=f"no Azurite answers at {LIVE_AZ}")
def test_a_live_existing_server_keeps_the_contract():
    check_contract(lambda run_token: AzureServiceDef.azurite().as_existing(address=LIVE_AZ).make(run_token=run_token))


# -----------------------------------------------------------------------------
# Its containers
#


@pytest.fixture
def data(tmp_path):
    """A local directory as the azure extension's `data/`."""
    root = tmp_path / "data"
    (root / "partitioned").mkdir(parents=True)
    (root / "l.csv").write_bytes(b"l_orderkey\n1\n")
    (root / "partitioned" / "data_0.csv").write_bytes(b"x\n")
    return str(root)


@pytest.fixture
def found(no_service_env, monkeypatch, store):
    """A server found running: what it holds is the fake store."""
    monkeypatch.setattr(azure_module, "docker", FakeDocker())
    monkeypatch.setattr(azure_module, "az_answers", lambda endpoint: True)
    store.containers["ducktest"] = {}
    return store


def test_containers_are_made_filled_and_opened_with_the_server_and_stay_on_one_found_running(found, data, caplog):
    definition = AzureServiceDef.azurite(
        containers=[
            container("testing-private", seed=[(data, "")]),
            container("testing-public", seed=[(data, "")], public="blob"),
            container("writes"),
        ]
    )
    made = Made("r1")
    service = made.create(definition)
    assert sorted(found.containers["testing-private"]) == ["l.csv", "partitioned/data_0.csv"]
    assert found.public == {"testing-public": "blob"}
    assert service.made_containers == ["testing-private", "testing-public", "writes"]
    with caplog.at_level(logging.INFO):
        made.end(failed=False)
        service.delete()
    assert sorted(found.containers) == ["ducktest", "testing-private", "testing-public", "writes"]
    assert [c for c in found.calls if c[0] == "delete_container"] == []
    stays = [r.getMessage() for r in caplog.records if "stays" in r.getMessage()]
    assert stays == [
        f"az: container {name!r} made by this run stays: the server was found running"
        for name in ("writes", "testing-public", "testing-private")
    ]


def test_containers_of_a_server_the_run_started_go_before_it_stops(fake_docker, store, data):
    service = AzureServiceDef.azurite(containers=[container("data", seed=[(data, "")])]).make(run_token="r1")
    service.create()
    service.end(failed=False)
    assert sorted(store.containers) == ["ducktest"] and fake_docker.containers == {}


def test_a_container_that_holds_what_it_seeds_is_used_as_it_is_and_left(found, data):
    found.containers["open"] = {**files_of(data), "theirs.csv": b"x"}
    found.public["open"] = "container"
    made = Made("r1")
    service = made.create(AzureServiceDef.azurite(containers=[container("open", seed=[(data, "")], public=True)]))
    assert service.made_containers == [] and [c for c in found.calls if c[1] == "open"] == []
    made.end(failed=False)
    assert "theirs.csv" in found.containers["open"]


@pytest.mark.parametrize("what", ["content", "public"])
def test_a_container_that_is_there_with_other_content_is_refused_and_not_touched(found, data, what):
    found.containers["data"] = files_of(data)
    if what == "content":
        found.containers["data"]["l.csv"] = b"changed"
    made = Made("r1")
    service = made.create(
        AzureServiceDef.azurite(containers=[container("data", seed=[(data, "")], public=what == "public")])
    )
    assert "does not change a container it did not make" in service.refused["data"]
    assert made.errors == {} and [c for c in found.calls if c[1] == "data"] == []


def test_a_container_that_fails_half_way_is_removed_and_the_others_stay(found, data):
    found.fail_on = "set_public_access"
    made = Made("r1")
    service = made.create(
        AzureServiceDef.azurite(containers=[container("data", seed=[(data, "")]), container("open", public=True)])
    )
    assert "set_public_access: AuthorizationFailure" in service.refused["open"] and list(service.refused) == ["open"]
    assert sorted(found.containers) == ["data", "ducktest"] and service.made_containers == ["data"]
    assert made.errors == {}, "the server stays up"


def test_a_seed_directory_that_is_not_there_makes_no_container(found, tmp_path):
    service = Made("r1").create(
        AzureServiceDef.azurite(containers=[container("data", seed=[(str(tmp_path / "gone"), "")])])
    )
    assert "no directory at" in service.refused["data"]
    assert [c for c in found.calls if c[0] != "create_container" or c[1] != "ducktest"] == []


def test_a_failure_to_make_its_own_container_means_the_server_is_not_up(found):
    found.fail_on = "create_container"
    made = Made("r1")
    with pytest.raises(ServiceNotUp, match="its container: RuntimeError"):
        made.create(AzureServiceDef.azurite())
    assert "az" in made.errors


def test_a_test_needs_the_containers_of_its_storages_or_every_container_when_it_binds_none():
    az = AzureServiceDef.azurite(containers=[container("data"), container("open")])
    data, own = az.storage("d", path="lake", container="data"), az.storage("own", path="lake")
    key = az.credential("key", scopes=["d", "own"])
    assert az.parts_needed([data, key]) == ["data"]
    assert az.parts_needed([own, key]) == []
    assert az.parts_needed([key]) == az.parts_needed([own, az]) == ["data", "open"]


@pytest.mark.parametrize("retain, kept", [("never", False), ("on_failure", True), ("always", True)])
def test_retain_keeps_a_made_container_after_a_failed_run(fake_docker, store, data, retain, kept):
    made = Made("r1")
    made.create(AzureServiceDef.azurite(containers=[container("data", seed=[(data, "")], retain=retain)]))
    made.end(failed=True)
    assert ("data" in store.containers) is kept


def test_a_public_level_is_true_blob_or_container():
    assert [container("c", public=p).public for p in (False, True, "blob", "container")] == [
        None,
        "container",
        "blob",
        "container",
    ]
    with pytest.raises(UsageError, match="public='all'"):
        container("c", public="all")


def test_its_containers_are_part_of_what_it_is(data):
    assert (
        AzureServiceDef.azurite().identity()
        != AzureServiceDef.azurite(containers=[container("data", seed=[(data, "")])]).identity()
    )


def test_reaching_the_server_after_the_run_makes_none_of_its_containers(found, data):
    definition = AzureServiceDef.azurite(containers=[container("data", seed=[(data, "")])])
    Made("r1").create(definition.reached_at("http://172.18.0.1:10000"))
    assert "data" not in found.containers


def test_a_storage_on_it_removes_what_the_run_wrote_through_rclone(found):
    definition = AzureServiceDef.azurite()
    service = Made("r1").create(definition)
    found.containers["ducktest"] = {"lake/temp/r1/k1/x.csv": b"1"}
    removed = []
    found.list = lambda uri: ["k1"]
    found.delete = removed.append
    storage = definition.storage("blobs", path="lake").make({"az": service}, run_token="r1")
    assert storage.clear("r1") == ["az://ducktest/lake/temp/r1/k1"] and removed == ["az://ducktest/lake/temp/r1/k1"]


def test_docker_is_never_called_with_the_well_known_key(fake_docker):
    AzureServiceDef.azurite().make(run_token="r1").create()
    assert not any(AZURITE_KEY in a for run in fake_docker.runs for a in run)
