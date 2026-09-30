"""Services on the resource base: the handler contract, an existing instance, retain, and what a
service gives its consumers."""

import os
import tempfile
import urllib.request
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from ducktest5 import phases
from ducktest5.resources import (
    ON_FAILURE,
    RO,
    SHARED_RW,
    Service,
    ServiceDef,
    ServiceNotUp,
    StorageDef,
    check_contract,
)
from ducktest5.resources.made import Made, services_in
from ducktest5.services.http import HttpServerDef

# What the fake kind's instances hold, by address: stands in for the world outside the process.
WORLD = {}


class Fake(Service):
    """Keeps `{address: running?}` in WORLD; its address carries the run_token."""

    @property
    def address(self):
        return self.definition.address or f"http://fake/{self.run_token}/{self.id}"

    def alloc(self):
        WORLD.setdefault(self.address, False)

    def start(self):
        if self.definition.broken:
            raise OSError("cannot start")
        WORLD[self.address] = True

    def stop(self):
        if self.address in WORLD:
            WORLD[self.address] = False

    def dealloc(self):
        WORLD.pop(self.address, None)

    def read(self):
        if self.address not in WORLD:
            return None
        return {"url": self.address, "key": "k-" + self.id}

    def ready(self):
        return WORLD.get(self.address, False)

    def list(self, run_token):
        return [a for a in WORLD if a.startswith(f"http://fake/{run_token}/")]

    @property
    def handle(self):
        return self.address


@dataclass(eq=False)
class FakeDef(ServiceDef):
    kind = "fake"
    attributes = ("url",)
    secret_attributes = ("key",)
    delivers = {"FAKE_KEY": "key", "FAKE_URL": "url"}
    ready_timeout_s = 1

    id: str
    broken: bool = False
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE


FakeDef.instance = Fake


@pytest.fixture(autouse=True)
def world():
    WORLD.clear()
    yield WORLD
    WORLD.clear()


def test_a_service_kind_keeps_the_contract():
    check_contract(lambda run_token: FakeDef("fake").make(run_token=run_token))


def test_a_service_is_shared_by_its_consumers():
    assert FakeDef.sharing == SHARED_RW
    service = FakeDef("fake").make(run_token="r1")
    service.create()
    assert service.acquire() is service and service.acquire() is service


def test_its_attributes_are_named_under_its_id_and_its_secrets_are_not_values():
    service = FakeDef("fake").make(run_token="r1")
    service.create()
    assert {k: o.get() for k, o in service.by_ref().items()} == {
        "fake.url": "http://fake/r1/fake",
        "fake.key": "k-fake",
    }
    assert list(service.attributes) == ["url"] and list(service.secrets) == ["key"]
    assert service.by_ref()["fake.url"].source == "service fake"


def test_a_start_that_fails_is_service_not_up_and_leaves_nothing_running(world):
    service = FakeDef("fake", broken=True).make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="^OSError: cannot start$"):
        service.create()
    assert world == {}


def test_retain_decides_whether_the_end_of_the_run_removes_what_it_holds(world):
    service = FakeDef("fake").make(run_token="r1")
    service.create()
    service.end(failed=True)
    assert world == {"http://fake/r1/fake": False}, "on_failure: stopped, kept"
    service.create()
    service.end(failed=False)
    assert world == {}


def test_an_existing_service_is_adopted_when_it_answers_and_never_removed(world):
    world["http://fake/there"] = True
    managed = FakeDef("fake")
    existing = managed.as_existing(address="http://fake/there")
    assert existing.identity() != managed.identity()
    service = existing.make(run_token="r1")
    assert not service.owned
    service.create()
    assert service.attributes["url"].get() == "http://fake/there"
    service.delete()
    service.end(failed=False)
    assert world == {"http://fake/there": True}
    check_contract(lambda run_token: existing.make(run_token=run_token))


def test_an_existing_service_that_does_not_answer_is_service_not_up(world):
    world["http://fake/there"] = False
    service = FakeDef("fake").as_existing(address="http://fake/there").make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="existing service 'fake' does not answer at http://fake/there"):
        service.create()
    assert world == {"http://fake/there": False}, "an existing service is never started"


def test_a_produced_credential_carries_the_services_secrets_under_the_names_it_delivers():
    definition = FakeDef("fake")
    credential_def = definition.credential("fake_keys", scopes=["bucket"])
    assert credential_def.scope_storages == ("bucket",)
    service = definition.make(run_token="r1")
    service.create()
    credential = credential_def.make({"fake": service})
    credential.create()
    credential.acquire(RO)
    assert {k: o.get() for k, o in credential.secrets.items()} == {
        "FAKE_KEY": "k-fake",
        "FAKE_URL": "http://fake/r1/fake",
    }
    assert credential.secrets["FAKE_KEY"].source == "service fake"
    assert service.delivery() == ({"FAKE_URL": service.attributes["url"]}, {}), "a service delivers no secret itself"


# -----------------------------------------------------------------------------
# The HTTP file server
#


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A source directory with one file, and the system temp dir in tmp_path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.csv").write_text("a\n1\n2\n")
    return str(source)


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode()


def test_the_http_server_keeps_the_contract(served):
    check_contract(lambda run_token: HttpServerDef("http", source=served).make(run_token=run_token))


def test_the_http_server_serves_a_copy_of_its_source_at_its_url(served):
    service = HttpServerDef("http", source=served).make(run_token="r1")
    service.create()
    try:
        url = service.by_ref()["http.url"].get()
        assert url.startswith("http://127.0.0.1:")
        assert get(f"{url}/data.csv") == "a\n1\n2\n"
        assert service.handle == os.path.join(tempfile.gettempdir(), "ducktest-http", "r1__http")
    finally:
        service.delete()


def test_list_finds_the_http_servers_directory_by_the_run_token_in_its_name(served):
    service = HttpServerDef("http", source=served).make(run_token="r1")
    service.create()
    try:
        assert service.list("r1") == [service.handle]
        assert service.list("r2") == []
    finally:
        service.delete()
    assert service.list("r1") == []


def test_an_http_server_that_cannot_bind_is_service_not_up(served):
    service = HttpServerDef("http", source=served, host="192.0.2.1").make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="^OSError: .*Cannot assign requested address"):
        service.create()
    service.delete()


def test_an_http_server_on_every_address_is_reached_on_the_loopback(served, monkeypatch):
    monkeypatch.setenv("DUCKTEST_SERVICE_BIND", "0.0.0.0")
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "192.0.2.1")  # where services elsewhere are reached; not this one
    service = HttpServerDef("http", source=served).make(run_token="r1")
    service.create()
    try:
        assert service.server.server_address[0] == "0.0.0.0"
        assert get(f"{service.attributes['url'].get()}/data.csv") == "a\n1\n2\n"
        assert service.attributes["url"].get().startswith("http://127.0.0.1:")
    finally:
        service.delete()


def test_an_existing_http_server_is_adopted_at_its_address(served):
    running = HttpServerDef("http", source=served).make(run_token="r1")
    running.create()
    try:
        url = running.attributes["url"].get()
        existing = HttpServerDef("http").as_existing(address=url).make(run_token="r2")
        existing.create()
        assert existing.attributes["url"].get() == url
        existing.delete()
        assert get(f"{url}/data.csv") == "a\n1\n2\n", "an existing server is never stopped"
    finally:
        running.delete()


# -----------------------------------------------------------------------------
# What a run made
#


def test_services_in_finds_the_services_behind_the_inputs_once_inputs_first():
    inner, outer = FakeDef("inner"), FakeDef("outer")
    outer.inputs = [inner]
    storages = [StorageDef("a", "{outer.url}", inputs=[outer]), StorageDef("b", "{inner.url}", inputs=[inner])]
    assert services_in(storages) == [inner, outer]


def test_every_consumer_of_a_service_gets_the_runs_one_instance_and_everything_carries_the_run_token():
    service = FakeDef("fake")
    storage = StorageDef("served", "{fake.url}", inputs=[service])
    made = Made("r1")
    made.create(service)
    first, second = made.make(storage), made.make(storage)
    assert first.inputs["fake"] is second.inputs["fake"] is made.services["fake"]
    assert first.root == "http://fake/r1/fake" and first.run_token == "r1"


def test_a_consumer_of_a_service_that_did_not_come_up_is_service_not_up_with_the_reason():
    made = Made("r1")
    with pytest.raises(ServiceNotUp):
        made.create(FakeDef("fake", broken=True))
    with pytest.raises(ServiceNotUp, match="^OSError: cannot start$"):
        made.make(StorageDef("served", "{fake.url}", inputs=[FakeDef("fake", broken=True)]))


def test_the_end_of_the_run_ends_every_service_it_made(world):
    made = Made("r1")
    made.create(FakeDef("fake"))
    made.end(failed=False)
    assert world == {} and made.services == {}


# -----------------------------------------------------------------------------
# Cleanup by run token
#


def _project(*services):
    storages = {f"s{n}": StorageDef(f"s{n}", f"{{{s.id}.url}}", inputs=[s]) for n, s in enumerate(services)}
    return SimpleNamespace(storages=storages, credentials={}, services={s.id: s for s in services})


def test_list_finds_what_a_run_made_by_its_run_token_and_cleanup_after_it_was_killed_removes_it(served, tmp_path):
    http = HttpServerDef("http", source=served)
    run_dir = tmp_path / "runs" / "2026-09-29T00-00-00Z--killed-run-1"
    run_dir.mkdir(parents=True)
    token = run_dir.name
    made = Made(token)
    service = made.create(http)
    service.stop()  # the process was killed: nothing ended the service, and its directory stays
    assert http.make(run_token=token).list(token) == [service.handle]

    summary = phases.cleanup(str(run_dir), project=_project(http), event_lines=[{"event": "run_start"}])
    assert summary["swept"] == [service.handle]
    assert http.make(run_token=token).list(token) == []


def test_cleanup_after_a_run_that_ended_leaves_what_retain_kept(served, tmp_path):
    http = HttpServerDef("http", source=served)
    run_dir = tmp_path / "runs" / "2026-09-29T00-00-00Z--failed-run-1"
    run_dir.mkdir(parents=True)
    made = Made(run_dir.name)
    service = made.create(http)
    made.end(failed=True)
    ended = [{"event": "run_start"}, {"event": "run_end"}]
    assert phases.cleanup(str(run_dir), project=_project(http), event_lines=ended)["swept"] == []
    assert os.path.isdir(service.handle)
