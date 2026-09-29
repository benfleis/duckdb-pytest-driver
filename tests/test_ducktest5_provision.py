"""ducktest5's service modes -- managed, existing, kept -- on the Service handler contract, and what
`03` records of each."""

import dataclasses

import pytest
from test_ducktest5_service import FakeDef

from ducktest5.binary import UsageError
from ducktest5.provision import (
    EXISTING,
    KEPT,
    MANAGED,
    adopt,
    attach_argument,
    bring_up,
    existing_address,
    record,
)
from ducktest5.resources.made import Made


@pytest.fixture(autouse=True)
def world():
    from test_ducktest5_service import WORLD

    WORLD.clear()
    yield WORLD
    WORLD.clear()


def up(world, address):
    world[address] = True
    return address


def test_managed_is_created_and_ended_with_the_run(world):
    made = Made("r1")
    assert bring_up(made, [FakeDef("fake")]) == {}
    assert made.modes == {"fake": MANAGED} and world == {"http://fake/r1/fake": True}
    made.end(failed=False)
    assert world == {}


def test_existing_is_adopted_at_its_address_and_never_ended(world):
    made = Made("r1")
    address = up(world, "http://fake/elsewhere")
    assert bring_up(made, [FakeDef("fake")], existing_services=[f"fake={address}"]) == {}
    assert made.modes == {"fake": EXISTING} and made.services["fake"].address == address
    made.end(failed=False)
    assert world == {address: True}


def test_a_service_declared_existing_is_adopted_without_being_named(world):
    made = Made("r1")
    address = up(world, "http://fake/declared")
    assert bring_up(made, [FakeDef("fake").as_existing(address)]) == {}
    assert made.modes == {"fake": EXISTING}


def test_existing_that_does_not_answer_is_not_up_and_is_never_started(world):
    events = []
    errors = bring_up(
        Made("r1"),
        [FakeDef("fake")],
        existing_services=["fake=http://fake/nowhere"],
        on=lambda i, **f: events.append(f["state"]),
    )
    assert "does not answer at http://fake/nowhere" in errors["fake"]
    assert events == ["starting", "failed"] and world == {}


def test_kept_is_retained_left_running_and_says_how_to_adopt_it(world):
    made = Made("r1")
    bring_up(made, [FakeDef("fake")], keep=True)
    service = made.services["fake"]
    assert made.modes == {"fake": KEPT} and service.definition.retain == "always"
    made.end(failed=False)
    assert world == {"http://fake/r1/fake": True}
    assert attach_argument(service) == "--existing-service fake=http://fake/r1/fake"


def test_a_separate_provision_refuses_a_service_that_lives_in_the_process():
    with pytest.raises(UsageError, match="lives in the process that starts it.*--existing-service fake=URL"):
        bring_up(Made("r1"), [FakeDef("fake")], separate=True)


def test_a_separate_provision_makes_a_detached_service():
    detached = dataclasses.make_dataclass("DetachedDef", [], bases=(FakeDef,), eq=False, namespace={"detached": True})
    made = Made("r1")
    assert bring_up(made, [detached("fake")], separate=True) == {}
    assert made.modes == {"fake": MANAGED}


@pytest.mark.parametrize(
    "entries, env, expected",
    [
        ([], None, None),
        (["store=http://h:9000"], None, "http://h:9000"),
        (["other=http://x"], None, None),
        ([], "http://from-env", "http://from-env"),
        (["store=http://cli"], "http://from-env", "http://cli"),
    ],
)
def test_how_an_existing_service_is_named(monkeypatch, entries, env, expected):
    monkeypatch.delenv("DUCKTEST_EXISTING_SERVICE_STORE", raising=False)
    if env:
        monkeypatch.setenv("DUCKTEST_EXISTING_SERVICE_STORE", env)
    assert existing_address("store", entries) == expected


@pytest.mark.parametrize("entry", ["store", "store="])
def test_an_existing_service_named_without_an_address_is_refused(entry):
    with pytest.raises(UsageError, match="name the address of the running instance"):
        existing_address("store", [entry])


def test_the_record_holds_the_address_and_the_attributes_with_every_secret_masked():
    made = Made("r1")
    bring_up(made, [FakeDef("fake")])
    service = made.services["fake"]
    assert record(service, MANAGED, owned=True) == {
        "kind": "fake",
        "identity": service.identity,
        "mode": MANAGED,
        "detached": False,
        "owned": True,
        "address": "http://fake/r1/fake",
        "attributes": {"url": "http://fake/r1/fake", "key": "***"},
    }


def test_exec_adopts_what_03_recorded_and_makes_here_what_lives_in_its_process(world):
    detached = dataclasses.make_dataclass("DetachedDef", [], bases=(FakeDef,), eq=False, namespace={"detached": True})
    address = up(world, "http://fake/provisioned")
    made = Made("r1")
    errors = adopt(
        made,
        [detached("store"), FakeDef("thread"), detached("absent")],
        {"store": {"address": address}},
    )
    assert made.modes == {"store": EXISTING, "thread": MANAGED}
    assert made.services["store"].address == address
    assert "no instance of 'absent' was provisioned" in errors["absent"]
