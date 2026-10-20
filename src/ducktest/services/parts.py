"""The parts a local object-store server holds for the run -- an S3 server's buckets, an Azure blob
server's containers -- and their lifecycle, the same for each protocol:

- one the run made goes at the end, unless its `retain` keeps it, or the server was found running:
  a part has a fixed name, so on a server the run did not start other runs may use it, and it stays;
- one already there that holds what it seeds (and is public as asked) is used as it is, never changed;
- one with other content, or that could not be made, is refused: the server stays up, and only the
  tests that need that part are blocked (`ServiceDef.parts_needed`, `Service.refused`).

The calls on the store are a protocol's own, behind `PartCalls`."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, ClassVar, Protocol

from ..resources import ALWAYS, NEVER, ON_FAILURE, ServiceDef, ServiceNotUp, StorageDef

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Part:
    """A part the server holds while the run uses it: `seed` copies each local directory to its
    prefix, `public` opens it to anonymous reads. A part the run made goes at the end, unless `retain`
    keeps it; it defaults to `never`, as a kept one would be used by the next run with whatever the
    tests wrote into it."""

    noun: ClassVar[str] = "part"

    name: str
    seed: tuple = ()  # ((local directory, prefix in the part), ...)
    public: bool | str | None = None
    retain: str = NEVER

    def __post_init__(self):
        from .. import names

        names.declare(self)

    def content(self):
        return {"name": self.name, "seed": [list(s) for s in self.seed], "public": self.public, "retain": self.retain}


class PartCalls(Protocol):
    """A protocol's calls on the parts of one server: its own API, and rclone for their files."""

    noun: str  # "bucket", "container"
    scheme: str  # of a URI in a part: "s3", "az"

    def exists(self, name: str) -> bool: ...
    def make(self, name: str) -> None: ...  # one that is there already is not an error
    def remove(self, name: str) -> None: ...  # with what is in it
    def is_public(self, part: Part) -> bool: ...  # as `part.public` asks
    def make_public(self, part: Part) -> None: ...
    def copy(self, source: str, uri: str) -> None: ...
    def check(self, source: str, uri: str) -> bool: ...  # `uri` holds each file of `source`


def parts_needed(definition: ServiceDef, inputs, names: list, scheme: str) -> list:
    """Of `names`, the parts a test with `inputs` needs: the part of each storage on `definition` it
    binds. One that binds no storage on it, or the server itself, may read any of them."""

    def on_it(d):
        return isinstance(d, ServiceDef) and d.name == definition.name

    storages = [d for d in inputs if isinstance(d, StorageDef) and any(on_it(sub) for _, _, sub in d.edges())]
    if not storages or any(on_it(d) for d in inputs):
        return list(names)
    return [
        n for n in names if any(d.root == f"{scheme}://{n}" or d.root.startswith(f"{scheme}://{n}/") for d in storages)
    ]


def retained(policy, failed):
    return policy == ALWAYS or (policy == ON_FAILURE and failed)


class Parts:
    """The `declared` parts of service `service_id`: which the run made, in order, and which it refused."""

    def __init__(self, service_id: str, declared: list):
        self.service_id, self.declared = service_id, list(declared)
        self.made: list = []  # names of the parts this instance made, in order
        self.refused: dict = {}  # name -> why the run does not use it

    def create(self, calls: PartCalls) -> None:
        """Make, fill and open each declared part; one that fails is refused, and what of it was made goes."""
        for p in self.declared:
            try:
                missing = [d for d, _ in p.seed if not os.path.isdir(d)]
                if missing:
                    raise ServiceNotUp(f"nothing to seed from: no directory at {', '.join(missing)}")
                self._make(calls, p)
            except Exception as exc:
                why = str(exc) if isinstance(exc, ServiceNotUp) else f"{type(exc).__name__}: {exc}"
                self.refused[p.name] = f"service {self.service_id}: {calls.noun} {p.name}: {why}"
                _log.info("%s: %s", calls.scheme, self.refused[p.name])
                self.remove(calls, lambda x: x.name == p.name)

    def _uri(self, calls, p, prefix):
        return f"{calls.scheme}://{p.name}/{prefix}".rstrip("/")

    def _differs(self, calls, p):
        """What in part `p` is not as declared, or None: it is."""
        for source, prefix in p.seed:
            if not calls.check(source, uri := self._uri(calls, p, prefix)):
                return f"{uri} does not hold what is in {source}"
        if p.public and not calls.is_public(p):
            return f"{calls.scheme}://{p.name} is not open to anonymous reads as declared ({p.public})"
        return None

    def _make(self, calls, p):
        if p.name not in self.made and calls.exists(p.name):
            if differs := self._differs(calls, p):
                raise ServiceNotUp(
                    f"it is there and {differs}; the run does not change a {calls.noun} it did not make. "
                    f"Remove it, or declare another {calls.noun}"
                )
            _log.info("%s: %s %s is there as declared, used as it is", calls.scheme, calls.noun, p.name)
            return
        if p.name in self.made and not self._differs(calls, p):
            return
        _log.info("%s: making %s %s", calls.scheme, calls.noun, p.name)
        calls.make(p.name)
        if p.name not in self.made:
            self.made.append(p.name)
        for source, prefix in p.seed:
            calls.copy(source, self._uri(calls, p, prefix))
        if p.public:
            calls.make_public(p)

    def remove(self, calls: PartCalls, goes: Callable[[Part], bool], *, found: bool = False) -> None:
        """Remove each part this instance made for which `goes(part)`, the last made first. On a server
        `found` running such a part stays, and is not the run's any more."""
        by_name = {p.name: p for p in self.declared}
        for name in reversed(list(self.made)):
            if not goes(by_name[name]):
                continue
            if found:
                _log.info(
                    "%s: %s %r made by this run stays: the server was found running", calls.scheme, calls.noun, name
                )
            else:
                _log.info("%s: removing %s %s, made by the run", calls.scheme, calls.noun, name)
                calls.remove(name)
            self.made.remove(name)

    def end(self, calls: PartCalls, failed: bool, *, found: bool = False) -> None:
        self.remove(calls, lambda p: not retained(p.retain, failed), found=found)
