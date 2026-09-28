"""The contract every kind keeps, as one call a kind's own tests make."""

from __future__ import annotations

import json
from typing import Callable

from .base import RW, Resource, canonical


def check_contract(make: Callable[[str], Resource], *, tag: str = "contract-run", key: str = "contract-test"):
    """Asserts the handler contract on the instances `make(tag)` returns: identity is the same for
    the same Def and inputs; create is safe to repeat; delete of what is not there is not an error;
    an owned instance is found by `list(tag)` after create and not after delete; a secret value is
    not in the Def's content. A kind that mints a child for a writer has the child checked too."""
    first, second = make(tag), make(tag)
    assert first.identity == second.identity, "one Def with the same inputs gave two identities"
    _check(first, first, tag)
    first.create()
    try:
        child = first.acquire(RW, key)
        if child is not first:
            assert child.owned, "a minted child is made by the run, so it is owned"
            assert first.acquire(RW, key) is child, "one key was given two children"
            _check(child, first, tag)
        first.release(child, RW)
    finally:
        first.delete()


def _check(instance: Resource, lister: Resource, tag: str):
    what = f"{instance.definition.kind} {instance.id}"
    instance.delete()  # nothing made yet, or made by an earlier failed attempt
    if instance.owned:
        assert instance.read() is None, f"{what}: read() found it before create()"
        assert instance.handle not in lister.list(tag), f"{what}: list(tag) found it before create()"
    instance.create()
    instance.create()
    assert instance.read() is not None, f"{what}: read() did not find it after create()"
    assert instance.ready(), f"{what}: not ready after create()"
    if instance.owned:
        assert instance.tag == tag, f"{what}: an owned instance carries the run's tag"
        assert instance.handle in lister.list(tag), f"{what}: list(tag) did not find it after create()"
    content = json.dumps(canonical(instance.definition.content()))
    for name, out in instance.secrets.items():
        value = out.get()
        assert not value or str(value) not in content, f"{what}: the value of secret {name!r} is in its content"
    instance.delete()
    instance.delete()
    if instance.owned:
        assert instance.read() is None, f"{what}: read() found it after delete()"
        assert instance.handle not in lister.list(tag), f"{what}: list(tag) found it after delete()"
    else:
        assert instance.read() is not None, f"{what}: delete() removed what the run did not make"
