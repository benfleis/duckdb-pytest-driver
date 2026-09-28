"""Resources: identity by content, ownership against capacity, outputs, and the handler contract."""

import subprocess
import sys
import threading
import time

import pytest

from ducktest5.resources import (
    EXCLUSIVE,
    NEVER,
    RO,
    RW,
    SHARED_RO,
    Output,
    Resource,
    check_contract,
)

# A small kind, as source text, so that another process can declare the very same one.
THING = """
import os
from dataclasses import dataclass, field
from ducktest5.resources import ResourceDef

@dataclass(eq=False)
class ThingDef(ResourceDef):
    kind = "thing"
    id: str
    size: int = 1
    make_path: object = os.path.join
    inputs: list = field(default_factory=list)
    retain: str = "on_failure"
"""
_space = {}
exec(THING, _space)
ThingDef = _space["ThingDef"]


# -----------------------------------------------------------------------------
# Identity
#


def test_identity_is_the_same_in_another_process():
    code = THING + "print(ThingDef('a', size=3).identity())"
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert other.stdout.strip() == ThingDef("a", size=3).identity()


def test_identity_is_content_the_name_is_a_label():
    assert ThingDef("a").identity() == ThingDef("b").identity()
    assert ThingDef("a", size=1).identity() != ThingDef("a", size=2).identity()


def test_identity_changes_with_an_input_identity_and_its_mode():
    d = ThingDef("a", inputs=["up"])
    assert d.identity({"up": "1"}) != d.identity({"up": "2"})
    assert ThingDef("a", inputs=[{"up": RO}]).identity({"up": "1"}) != d.identity({"up": "1"})


def test_an_input_not_fulfilled_has_no_identity():
    with pytest.raises(ValueError, match=r"inputs \['up'\] are not fulfilled"):
        ThingDef("a", inputs=["up"]).identity()


def test_an_input_may_be_a_def_object():
    up = ThingDef("up")
    assert ThingDef("a", inputs=[up]).edges() == [("up", RW, up)]
    assert ThingDef("a", inputs=[{up: RO}]).edges() == [("up", RO, up)]


def test_the_acquire_mode_and_a_nonce_split_identity():
    d = ThingDef("a")
    assert d.identity(mode=RW) != d.identity(mode=RO)
    assert d.identity(nonce="t1") != d.identity(nonce="t2") != d.identity()


def test_make_gives_the_instance_the_identity_of_its_fulfilled_inputs():
    up = ThingDef("up").make(tag="run1")
    a = ThingDef("a", inputs=["up"]).make({"up": up}, tag="run1")
    assert a.identity == ThingDef("a", inputs=["up"]).identity({"up": up.identity})
    assert a.tag == "run1" and a.inputs == {"up": up}


# -----------------------------------------------------------------------------
# Ownership
#


def _kind(capacity, mint=None):
    class Instance(Resource):
        def mint_child(self, token):
            return mint(self, token) if mint else None

    return type("K", (ThingDef,), {"capacity": capacity, "instance": Instance})


def test_a_writer_of_an_exclusive_kind_waits_for_the_one_before_it():
    thing = _kind(EXCLUSIVE)("a").make()
    held = thing.acquire(RW, "t1")
    assert held is thing
    order = []

    def second():
        thing.release(thing.acquire(RW, "t2"), RW)
        order.append("second")

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.1)
    order.append("first")
    thing.release(held, RW)
    t.join(2)
    assert order == ["first", "second"]


def test_readers_of_an_exclusive_kind_share_it():
    thing = _kind(EXCLUSIVE)("a").make()
    assert thing.acquire(RO) is thing and thing.acquire(RO) is thing


def test_an_exclusive_writer_gets_a_minted_child_per_token_instead_of_a_lock():
    kind = _kind(EXCLUSIVE, mint=lambda parent, token: ThingDef("c").make(tag=parent.tag, nonce=token))
    thing = kind("a").make(tag="run1")
    one, two = thing.acquire(RW, "t1"), thing.acquire(RW, "t2")
    assert one is not thing and one is not two
    assert thing.acquire(RW, "t1") is one
    assert thing.children == {"t1": one, "t2": two}


def test_a_read_only_kind_serves_a_writer_as_a_reader():
    thing = _kind(SHARED_RO)("a").make()
    assert thing.acquire(RW) is thing and thing.acquire(RW) is thing


def test_an_expired_instance_is_renewed_at_acquire():
    thing = ThingDef("a").make()
    renewed = []
    thing.expires, thing.renew = (lambda: time.time() - 1), (lambda: renewed.append(1))
    thing.acquire(RO)
    assert renewed == [1]


def test_retain_says_when_the_run_leaves_it_in_place():
    assert ThingDef("a").make().retained(failed=True)
    assert not ThingDef("a").make().retained(failed=False)
    assert not ThingDef("a", retain=NEVER).make().retained(failed=True)


# -----------------------------------------------------------------------------
# Outputs
#


def test_outputs_are_named_under_the_producer_and_delivered_as_the_kind_names_them():
    thing = ThingDef("a").make()
    thing.env["ENDPOINT"] = Output("http://x", "thing a")
    thing.secrets["KEY"] = Output(lambda: "k", "thing a")
    assert set(thing.outputs()) == {"a.ENDPOINT", "a.KEY"}
    env, secrets = thing.delivery()
    assert env["ENDPOINT"].get() == "http://x" and secrets["KEY"].get() == "k"


def test_an_output_of_none_unsets_the_name():
    assert Output(None, "x").unset and not Output("", "x").unset


# -----------------------------------------------------------------------------
# The contract helper
#


def test_the_contract_helper_fails_a_kind_whose_delete_of_a_missing_instance_raises():
    class Fragile(Resource):
        def dealloc(self):
            raise FileNotFoundError("not there")

    kind = type("F", (ThingDef,), {"instance": Fragile})
    with pytest.raises(FileNotFoundError):
        check_contract(lambda tag: kind("a").make(tag=tag))


def test_the_contract_helper_passes_the_base_kind():
    check_contract(lambda tag: ThingDef("a").make(tag=tag))
