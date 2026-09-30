"""Storages: a location a test reads from and writes under, local or remote."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Mapping

from ..binary import UsageError
from .base import SHARED_RW, Attribute, Resource, ResourceDef, digest

SCHEMES = ("file", "s3", "az", "abfss", "http", "https")
_REF = re.compile(r"\{([\w-]+)\.(\w+)\}")
RUN_TOKEN_PENDING = "<run_token>"  # in a run root shown before there is a run


def local_path(root: str) -> str:
    """The path of a local root: a plain path, `file:///path` or `file://localhost/path`."""
    for prefix in ("file://localhost/", "file:///"):
        if root.startswith(prefix):
            return "/" + root[len(prefix) :]
    if root.startswith("file://"):
        raise UsageError(f"storage root {root!r}: write a plain path, file:///path or file://localhost/path")
    return root


def scheme_of(root: str) -> str:
    """The scheme of a root; a plain path is `file`."""
    if "://" not in root:
        return "file"
    scheme = root.split("://", 1)[0].lower()
    if scheme not in SCHEMES:
        raise UsageError(f"storage root {root!r}: scheme {scheme!r} is not one of {SCHEMES}")
    return scheme


def fill(template: str, inputs: Mapping[str, Resource]) -> str:
    """`template` with each `{input.name}` replaced by that input's attribute `name`."""

    def one(m):
        ref, name = m.groups()
        if ref not in inputs or name not in inputs[ref].attributes:
            raise UsageError(f"{template!r}: {{{ref}.{name}}} is not an attribute of an input")
        return str(inputs[ref].attributes[name].get())

    return _REF.sub(one, template)


@dataclass(eq=False)
class StorageDef(ResourceDef):
    """A location: a local directory, a bucket, a container, with the scheme its `root` says. The
    storage itself is shared; a writer with a key gets a child of its own, a prefix under the root;
    a writer with none has the whole root to itself, under a lock. Its attributes are its roots and
    endpoint, delivered as the binary's roots (`TEMP_DIR`, `DATA_DIR` and their `LOCAL_` twins)
    and `S3_ENDPOINT`, `S3_USE_SSL`, `AZ_ACCOUNT` where they apply.

    Its temp roots have three levels: the root, bound to the storage; `<root>/<temp prefix>/<run
    token>`, bound to the run (`TEMP_DIR_ROOT`); and a child minted under that with a test's key
    (`TEMP_DIR`). `data` is where fixtures are read, the same in every run; a storage that declares
    none gives no `DATA_DIR`, and a test that binds it reads the default storage's."""

    kind = "storage"
    sharing = SHARED_RW
    mints_children = True

    id: str
    root: str  # a URI or a path; `{minio.bucket}` takes an input's attribute
    endpoint: str | None = None  # http(s)://host:port of an S3-compatible store; may reference an input
    account: str | None = None  # the Azure account, where the root does not name it
    temp_prefix: str = "temp"  # where children are minted, under the root
    inputs: list = field(default_factory=list)
    retain: str = "on_failure"
    data: str | None = None  # a URI or a path, often the root; may reference an input


class Storage(Resource):
    """A storage with its root filled in. A child is owned: the run removes it, where the scheme
    has a way to remove things."""

    def __init__(self, definition, identity, run_token, inputs=None, *, parent=None, root=None):
        super().__init__(definition, identity, run_token, inputs)
        self.parent = parent
        self.root = (root or fill(definition.root, self.inputs)).rstrip("/")
        self.scheme = scheme_of(self.root)
        endpoint = fill(definition.endpoint, self.inputs) if definition.endpoint else None
        data = fill(definition.data, self.inputs).rstrip("/") if definition.data else None
        account = definition.account
        if account is None and self.scheme == "abfss" and "@" in self.root:
            account = self.root.split("@", 1)[1].split(".", 1)[0]
        source = f"storage {self.id}"
        facts = {
            "root": self.root,
            "scheme": self.scheme,
            "data_root": data,
            "temp_root": self.root if parent else self.run_root(run_token or RUN_TOKEN_PENDING),
            "endpoint": endpoint,
            "account": account,
        }
        self.attributes = {name: Attribute(value, source) for name, value in facts.items()}

    @property
    def owned(self):
        return self.parent is not None

    @property
    def handle(self):
        return self.root

    @property
    def path(self) -> str:
        """The local path of a `file` root."""
        return local_path(self.root)

    def run_root(self, run_token):
        """Where everything the run `run_token` makes under this storage is."""
        return f"{self.root}/{self.definition.temp_prefix}/{run_token}"

    def mint_child(self, key):
        if self.run_token is None:
            raise ValueError(f"storage {self.id}: a child needs the run's run_token")
        root = f"{self.run_root(self.run_token)}/{key}"
        return Storage(
            self.definition, digest([self.identity, key]), self.run_token, self.inputs, parent=self, root=root
        )

    def remote(self):
        """The operations on the store of an s3 root, from the service it is on; None when it is on
        none, and so the run cannot remove what it wrote there."""
        if self.scheme != "s3":
            return None
        return next((i.remote() for i in self.inputs.values() if callable(getattr(i, "remote", None))), None)

    def _names_under(self, uri):
        if self.scheme == "file":
            path = local_path(uri)
            return sorted(os.listdir(path)) if os.path.isdir(path) else []
        remote = self.remote()
        return sorted(remote.list(uri)) if remote else []

    def _remove(self, uri):
        if self.scheme == "file":
            shutil.rmtree(local_path(uri), ignore_errors=True)
        elif (remote := self.remote()) is not None:
            remote.delete(uri)

    def alloc(self):
        if self.scheme == "file":
            os.makedirs(self.path if self.owned else local_path(self.attributes["temp_root"].value), exist_ok=True)

    def dealloc(self):
        if self.owned:  # the root was declared, not made: it is never removed
            self._remove(self.root)

    def sweep(self, run_token):
        """Remove what the run `run_token` left under this storage; returns what `list` found."""
        found = self.list(run_token)
        if found:
            self._remove(self.run_root(run_token))
        return found

    def clear(self, run_token, keep=()):
        """At the end of a run: remove what the run `run_token` left under this storage but the
        places in `keep` and what holds them, then the run's own directory and the temp prefix, where
        they are empty; an object store has no directories to remove. Returns what it removed."""
        if self.scheme != "file":
            under = self.run_root(run_token)
            doomed = [f"{under}/{n}" for n in self._names_under(under)]
            doomed = [uri for uri in doomed if not any(k == uri or k.startswith(uri + "/") for k in keep)]
            for uri in doomed:
                self._remove(uri)
            return doomed
        under = local_path(self.run_root(run_token))
        keep = [os.path.abspath(k) for k in keep]
        removed = []
        for name in self._names_under(under):
            path = os.path.join(under, name)
            if not any(k == path or k.startswith(path + os.sep) for k in keep):
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
        for directory in (under, os.path.dirname(under)):
            try:
                os.rmdir(directory)
                removed.append(directory)
            except OSError:  # not empty, or not there
                break
        return removed

    def read(self):
        if self.scheme == "file" and not os.path.isdir(self.path):
            return None
        return {"id": self.id, "root": self.root}

    def ready(self):
        return self.scheme != "file" or os.path.isdir(self.path)

    def list(self, run_token):
        under = self.run_root(run_token)
        return [f"{under}/{n}" for n in self._names_under(under)]

    def delivery(self):
        facts = {name: a.value for name, a in self.attributes.items()}
        out = {"TEMP_DIR": facts["temp_root"]}
        if self.scheme == "file":
            out["TEMP_DIR"] = out["LOCAL_TEMP_DIR"] = local_path(out["TEMP_DIR"])
        if facts["data_root"] is not None:
            out["DATA_DIR"] = facts["data_root"]
            if scheme_of(out["DATA_DIR"]) == "file":
                out["DATA_DIR"] = out["LOCAL_DATA_DIR"] = local_path(out["DATA_DIR"])
        if self.scheme == "s3" and facts["endpoint"]:
            out["S3_ENDPOINT"] = facts["endpoint"].split("://", 1)[-1]
            out["S3_USE_SSL"] = "true" if facts["endpoint"].startswith("https://") else "false"
        if self.scheme in ("az", "abfss") and facts["account"]:
            out["AZ_ACCOUNT"] = facts["account"]
        source = f"storage {self.id}"
        return {name: Attribute(value, source) for name, value in out.items()}, {}


StorageDef.instance = Storage


@dataclass(eq=False)
class DefaultStorageDef(StorageDef):
    """The run's default storage, declared: a project file may name it in STORAGES to say what its
    tests use. Binding it is binding nothing: the test gets the default storage's roots."""

    root: str = "(the run's default storage)"


def default_storage(id="default") -> DefaultStorageDef:
    """The storage a test gets when it binds none: `DATA_DIR` is `data/` at the top of the
    repository, and the test's `TEMP_DIR` is its own place in the run directory."""
    return DefaultStorageDef(id)


DEFAULT_TEMP_ROOT = "duckdb_unittest_tempdir"


@dataclass(frozen=True)
class DefaultStorage:
    """The storage of a test that binds none, one for the run: fixtures are read from `data_dir`, and
    what the run writes is under `<temp_root>/<run_token>`, the run directory, where a Python body's
    own place is `tests/<key>`; a `.test` body's is `<batch_token>/<test id>`, made by the binary."""

    data_dir: str
    temp_root: str = DEFAULT_TEMP_ROOT

    def run_root(self, run_token):
        return os.path.join(os.path.abspath(self.temp_root), run_token or RUN_TOKEN_PENDING)

    def test_dir(self, run_token, key):
        return os.path.join(self.run_root(run_token), "tests", key)

    def with_(self, *, data_dir=None, temp_root=None):
        """A copy with the values given, made absolute; one left None stays."""
        given = lambda path, kept: os.path.abspath(path) if path else kept  # noqa: E731
        return DefaultStorage(given(data_dir, self.data_dir), given(temp_root, self.temp_root))
