"""Services on the resource base: the handler contract, an existing instance, retain, and what a
service gives its consumers."""

from dataclasses import dataclass, field

import pytest

from ducktest5.resources import ON_FAILURE, RO, SHARED_RW, Service, ServiceDef, ServiceNotUp, check_contract

# What the fake kind's instances hold, by address: stands in for the world outside the process.
WORLD = {}


class Fake(Service):
    """Keeps `{address: running?}` in WORLD; its address carries the run_token."""

    @property
    def address(self):
        return self.definition.address or f"fake://{self.run_token}/{self.id}"

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
        return [a for a in WORLD if a.startswith(f"fake://{run_token}/")]

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


def test_its_attributes_are_outputs_under_its_id_and_its_secrets_are_not_env():
    service = FakeDef("fake").make(run_token="r1")
    service.create()
    assert {k: o.get() for k, o in service.outputs().items()} == {"fake.url": "fake://r1/fake", "fake.key": "k-fake"}
    assert list(service.env) == ["url"] and list(service.secrets) == ["key"]
    assert service.outputs()["fake.url"].source == "service fake"


def test_a_start_that_fails_is_service_not_up_and_leaves_nothing_running(world):
    service = FakeDef("fake", broken=True).make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="did not come up: cannot start"):
        service.create()
    assert world == {"fake://r1/fake": False}


def test_retain_decides_whether_the_end_of_the_run_removes_what_it_holds(world):
    service = FakeDef("fake").make(run_token="r1")
    service.create()
    service.end(failed=True)
    assert world == {"fake://r1/fake": False}, "on_failure: stopped, kept"
    service.create()
    service.end(failed=False)
    assert world == {}


def test_an_existing_service_is_adopted_when_it_answers_and_never_removed(world):
    world["fake://there"] = True
    managed = FakeDef("fake")
    existing = managed.as_existing(address="fake://there")
    assert existing.identity() != managed.identity()
    service = existing.make(run_token="r1")
    assert not service.owned
    service.create()
    assert service.env["url"].get() == "fake://there"
    service.delete()
    service.end(failed=False)
    assert world == {"fake://there": True}
    check_contract(lambda run_token: existing.make(run_token=run_token))


def test_an_existing_service_that_does_not_answer_is_service_not_up(world):
    world["fake://there"] = False
    service = FakeDef("fake").as_existing(address="fake://there").make(run_token="r1")
    with pytest.raises(ServiceNotUp, match="existing service 'fake' does not answer at fake://there"):
        service.create()
    assert world == {"fake://there": False}, "an existing service is never started"


def test_a_produced_credential_carries_the_services_secrets_under_the_names_it_delivers():
    definition = FakeDef("fake")
    credential_def = definition.credential("fake_keys", scopes=["bucket"])
    assert credential_def.scope_storages == ("bucket",)
    service = definition.make(run_token="r1")
    service.create()
    credential = credential_def.make({"fake": service})
    credential.create()
    credential.acquire(RO)
    assert {k: o.get() for k, o in credential.secrets.items()} == {"FAKE_KEY": "k-fake", "FAKE_URL": "fake://r1/fake"}
    assert credential.secrets["FAKE_KEY"].source == "service fake"
    assert service.delivery() == ({"FAKE_URL": service.env["url"]}, {}), "a service delivers no secret itself"
