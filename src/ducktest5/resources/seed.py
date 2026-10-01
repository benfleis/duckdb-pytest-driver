"""Seeds: a bucket a store must hold, with its content, before the tests that need it run.

A seed is a service kind for its lifecycle: the run makes it once, after the service it is on, a test
that needs it binds it, and one that does not come up makes those tests BLOCKED. Its input is a
service with a store (`remote()`, as `s3_local()` has); `S3.seed(...)` declares one on it.

The unit the run owns is the bucket. A bucket that is not there is made, filled and removed at the
end unless `retain` keeps it. One that is there and holds what the seed copies (and is public when
the seed says so) is used as it is and never changed. One that is there with other content is
refused: the run does not write into a bucket it did not make."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .base import NEVER, Attribute
from .service import Service, ServiceDef, ServiceNotUp

_log = logging.getLogger(__name__)


@dataclass(eq=False)
class SeedDef(ServiceDef):
    """`bucket` on the store of its input service, holding each local directory of `copy` under its
    prefix: `copy=[("/path/to/dat", "dat")]`. `public` opens the bucket to anonymous list and read.
    With no `copy` it is an empty bucket. Its attribute is `uri`, the bucket's."""

    kind = "seed"
    detached = True  # what it made is in the store, which outlives the process
    attributes = ("uri",)

    id: str
    bucket: str = ""
    copy: list = field(default_factory=list)  # (local directory, prefix in the bucket)
    public: bool = False
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = NEVER  # a seed is input, not evidence: a kept one would be adopted, with what tests wrote


def seed(service: ServiceDef, id: str, bucket: str, copy=(), public: bool = False, **fields) -> SeedDef:
    """A seed on `service`; see SeedDef."""
    return SeedDef(id, bucket=bucket, copy=[tuple(c) for c in copy], public=public, inputs=[service], **fields)


class Seed(Service):
    _made = False  # this instance made the bucket; only then does it remove it

    @property
    def uri(self):
        return f"{self._store().access.scheme}://{self.definition.bucket}"

    @property
    def address(self):
        return self.definition.address or self.uri

    @property
    def handle(self):
        return self.uri

    def _store(self):
        stores = [i for i in self.inputs.values() if callable(getattr(i, "remote", None))]
        if not stores:
            raise ServiceNotUp(f"seed {self.id!r}: none of its inputs is a service with a store")
        return stores[0].remote()

    def _target(self, prefix):
        return f"{self.uri}/{prefix}".rstrip("/")

    def _differs(self, store):
        """What in the bucket is not as the seed says, or None: it is."""
        for source, prefix in self.definition.copy:
            if not store.check(source, self._target(prefix)):
                return f"{self._target(prefix)} does not hold what is in {source}"
        if self.definition.public and not store.is_public(self.definition.bucket):
            return f"{self.uri} is not open to anonymous reads"
        return None

    def _sources(self):
        missing = [source for source, _ in self.definition.copy if not os.path.isdir(source)]
        if missing:
            raise ServiceNotUp(f"seed {self.id!r}: no directory at {', '.join(missing)}")

    def create(self):
        store = self._store()
        self._sources()
        if not store.bucket_exists(self.definition.bucket):
            if self.existing:
                raise ServiceNotUp(f"existing seed {self.id!r}: there is no bucket {self.uri}")
            self._fill(store)
        elif differs := self._differs(store):
            if not self._made:
                raise ServiceNotUp(
                    f"seed {self.id!r}: the bucket is there and {differs}; the run does not change a bucket it "
                    f"did not make. Remove it, or point the seed at another bucket"
                )
            self._fill(store)
        elif not self._made and not self.existing:
            self.found = True
            _log.info("seed %s: %s is there as declared, used as it is", self.id, self.uri)
        self.attributes = {"uri": Attribute(self.uri, f"seed {self.id}")}

    def _fill(self, store):
        _log.info("seed %s: making %s", self.id, self.uri)
        self._made = True
        try:
            store.make_bucket(self.definition.bucket)
            for source, prefix in self.definition.copy:
                store.copy(source, self._target(prefix))
            if self.definition.public:
                store.make_public(self.definition.bucket)
        except Exception as exc:
            self.dealloc()
            raise ServiceNotUp(f"seed {self.id!r}: {type(exc).__name__}: {exc}") from exc

    def dealloc(self):
        if self._made and not self.existing:
            self._store().delete_bucket(self.definition.bucket)
            self._made = False

    def read(self):
        return {"uri": self.uri} if self._store().bucket_exists(self.definition.bucket) else None

    def list(self, run_token):
        return [self.uri] if self._made and self.read() is not None else []


SeedDef.instance = Seed
