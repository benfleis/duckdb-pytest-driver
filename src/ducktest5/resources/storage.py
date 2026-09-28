"""Storages: a location a test reads from and writes under, local or remote."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Mapping

from ..binary import UsageError
from .base import EXCLUSIVE, Output, Resource, ResourceDef, digest

SCHEMES = ("file", "s3", "az", "abfss", "http", "https")
_REF = re.compile(r"\{([\w-]+)\.(\w+)\}")


def scheme_of(root: str) -> str:
    """The scheme of a root; a plain path is `file`."""
    if "://" not in root:
        return "file"
    scheme = root.split("://", 1)[0].lower()
    if scheme not in SCHEMES:
        raise UsageError(f"storage root {root!r}: scheme {scheme!r} is not one of {SCHEMES}")
    return scheme


def fill(template: str, inputs: Mapping[str, Resource]) -> str:
    """`template` with each `{input.name}` replaced by that input's output `name`."""

    def one(m):
        ref, name = m.groups()
        if ref not in inputs or name not in inputs[ref].env:
            raise UsageError(f"{template!r}: {{{ref}.{name}}} is not an output of an input")
        return str(inputs[ref].env[name].get())

    return _REF.sub(one, template)


@dataclass(eq=False)
class StorageDef(ResourceDef):
    """A location: a local directory, a bucket, a container, with the scheme its `root` says. A
    reader shares it; a writer with a test token gets a child of its own, a prefix under the root;
    a writer with none has the whole root to itself, under a lock. It outputs its roots and
    endpoint, delivered as the binary's roots (`TEMP_DIR`, `DATA_DIR` and their `LOCAL_` twins)
    and `S3_ENDPOINT`, `S3_USE_SSL`, `AZ_ACCOUNT` where they apply."""

    kind = "storage"
    capacity = EXCLUSIVE

    id: str
    root: str  # a URI or a path; `{minio.bucket}` takes an input's output
    endpoint: str | None = None  # http(s)://host:port of an S3-compatible store; may reference an input
    account: str | None = None  # the Azure account, where the root does not name it
    temp_prefix: str = "temp"  # where children are minted, under the root
    inputs: list = field(default_factory=list)
    retain: str = "on_failure"


class Storage(Resource):
    """A storage with its root filled in. A child is owned: the run removes it, where the scheme
    has a way to remove things."""

    def __init__(self, definition, identity, tag, inputs=None, *, parent=None, root=None):
        super().__init__(definition, identity, tag, inputs)
        self.parent = parent
        self.root = (root or fill(definition.root, self.inputs)).rstrip("/")
        self.scheme = scheme_of(self.root)
        endpoint = fill(definition.endpoint, self.inputs) if definition.endpoint else None
        account = definition.account
        if account is None and self.scheme == "abfss" and "@" in self.root:
            account = self.root.split("@", 1)[1].split(".", 1)[0]
        source = f"storage {self.id}"
        facts = {
            "root": self.root,
            "scheme": self.scheme,
            "data_root": parent.root if parent else self.root,
            "temp_root": self.root if parent else f"{self.root}/{definition.temp_prefix}",
            "endpoint": endpoint,
            "account": account,
        }
        self.env = {name: Output(value, source) for name, value in facts.items()}

    @property
    def owned(self):
        return self.parent is not None

    @property
    def handle(self):
        return self.root

    @property
    def path(self) -> str:
        """The local path of a `file` root."""
        return self.root[len("file://") :] if self.root.startswith("file://") else self.root

    def mint_child(self, token):
        if self.tag is None:
            raise ValueError(f"storage {self.id}: a child needs the run's tag")
        root = f"{self.root}/{self.definition.temp_prefix}/{self.tag}/{token}"
        return Storage(self.definition, digest([self.identity, token]), self.tag, self.inputs, parent=self, root=root)

    def alloc(self):
        if self.scheme == "file":
            os.makedirs(self.path if self.owned else f"{self.path}/{self.definition.temp_prefix}", exist_ok=True)

    def dealloc(self):
        if not self.owned:
            return  # the root was declared, not made: it is never removed
        if self.scheme == "file":
            shutil.rmtree(self.path, ignore_errors=True)
        # TODO: remove an s3 child, as services/minio.py `_clean` does, once the storage has its credential.

    def read(self):
        if self.scheme == "file" and not os.path.isdir(self.path):
            return None
        return {"id": self.id, "root": self.root}

    def ready(self):
        return self.scheme != "file" or os.path.isdir(self.path)

    def list(self, tag):
        if self.scheme != "file":
            return []  # TODO: list the prefixes under `<root>/<temp prefix>/<tag>` of a remote store
        under = f"{self.path}/{self.definition.temp_prefix}/{tag}"
        names = sorted(os.listdir(under)) if os.path.isdir(under) else []
        return [f"{self.root}/{self.definition.temp_prefix}/{tag}/{n}" for n in names]

    def delivery(self):
        facts = {name: out.value for name, out in self.env.items()}
        out = {"TEMP_DIR": facts["temp_root"], "DATA_DIR": facts["data_root"]}
        if self.scheme == "file":
            out = {k: v[len("file://") :] if v.startswith("file://") else v for k, v in out.items()}
            out["LOCAL_TEMP_DIR"], out["LOCAL_DATA_DIR"] = out["TEMP_DIR"], out["DATA_DIR"]
        if self.scheme == "s3" and facts["endpoint"]:
            out["S3_ENDPOINT"] = facts["endpoint"].split("://", 1)[-1]
            out["S3_USE_SSL"] = "true" if facts["endpoint"].startswith("https://") else "false"
        if self.scheme in ("az", "abfss") and facts["account"]:
            out["AZ_ACCOUNT"] = facts["account"]
        source = f"storage {self.id}"
        return {name: Output(value, source) for name, value in out.items()}, {}


StorageDef.instance = Storage
