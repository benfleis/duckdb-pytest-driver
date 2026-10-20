"""Storages: a location a test reads from and writes under, local or remote."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import ClassVar, Mapping

from ..binary import UsageError
from .base import SHARED_RW, Attribute, Resource, ResourceDef, digest

SCHEMES = ("file", "s3", "az", "abfss", "http", "https")
_REF = re.compile(r"\{([\w-]+)\.(\w+)\}")
ATTRIBUTES = ("root", "scheme", "data_root", "temp_root", "endpoint", "account")  # of every storage
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


def declared_scheme(root: str) -> str | None:
    """The scheme a declared root says; None when a template gives it, known only when the storage is made."""
    scheme = root.split("://", 1)[0].lower() if "://" in root else "file"
    return None if root.startswith("{") or "{" in scheme else scheme


def fill(template: str, inputs: Mapping[str, Resource]) -> str:
    """`template` with each `{input.name}` replaced by that input's attribute `name`."""

    def one(m):
        ref, name = m.groups()
        if ref not in inputs or name not in inputs[ref].attributes:
            raise UsageError(f"{template!r}: {{{ref}.{name}}} is not an attribute of an input")
        return str(inputs[ref].attributes[name].get())

    return _REF.sub(one, template)


def place_path(path, given, under="storage", *, empty=False) -> str:
    """A `path` under something rooted, `given` as the call says: relative, without `.`, `..` or `//`;
    `""`, the root itself, where `empty`."""
    if empty and path == "":
        return path
    if not isinstance(path, str) or "://" in path or path.startswith("/"):
        raise UsageError(f"{given}: give a path relative to the {under}, such as 'ducktest'")
    parts = path.strip("/").split("/")
    if not path.strip("/") or any(p in ("", ".", "..") for p in parts):
        raise UsageError(f"{given}: give a path below the {under}, without '.', '..' or '//'")
    return "/".join(parts)


@dataclass(frozen=True)
class At:
    """A place under a storage, `STORAGE.at("sub/path")`: what `data=` and `temp=` take besides a
    storage. `storage` is the declaration, or its name once a slot took it."""

    storage: object
    path: str

    @property
    def name(self) -> str:
        return self.storage if isinstance(self.storage, str) else self.storage.name

    def __repr__(self):
        return f"{self.name}.at({self.path!r})"

    __str__ = __repr__


@dataclass(eq=False)
class StorageDef(ResourceDef):
    """A location: a local directory, a bucket, a container, with the scheme its `root` says. The typed
    kinds say the protocol and check the root: `LocalStorageDef`, `S3StorageDef`, `AzureStorageDef`;
    `StorageDef` itself takes any root, such as a URL a service serves. The storage itself is shared; a
    writer with a key gets a child of its own, a path under the root; a writer with none has the whole
    root to itself, under a lock. A test gets from it what the slots that bind it say (`options.py`):
    bound by `storage=`, its values, `S3_ENDPOINT`, `S3_USE_SSL`, `AZ_ACCOUNT` where they apply; by
    `data=`, `DATA_DIR`, its root; by `temp=`, `TEMP_DIR`.

    Its temp roots have three levels: the place, `<root>/<temp_path>`, or `<root>/<path>` for
    `STORAGE.at(path)`; `<place>/<run token>`, bound to the run (`TEMP_DIR_ROOT`); and a child under
    that with a test's key (`TEMP_DIR`). `STORAGE.at(path)` bound by `data=` is `<root>/<path>`."""

    kind = "storage"
    sharing = SHARED_RW
    mints_children = True

    name: str
    root: str  # a URI or a path; `{local_s3.bucket}` takes an input's attribute
    endpoint: str | None = None  # http(s)://host:port of an S3-compatible store; may reference an input
    account: str | None = None  # the Azure account, where the root does not name it; may reference an input
    temp_path: str = "temp"  # the place of `temp=STORAGE`, under the root
    inputs: list = field(default_factory=list)
    retain: str = "on_failure"

    schemes: ClassVar[tuple | None] = None  # what a typed kind's `root` may be; None: any, checked when made

    def __post_init__(self):
        scheme = declared_scheme(self.root)
        if scheme is not None:
            self.check_scheme(scheme, self.root)
        self.temp_path = place_path(
            self.temp_path, f"{type(self).__name__}({self.name!r}, temp_path={self.temp_path!r})"
        )
        super().__post_init__()

    def check_scheme(self, scheme: str, root: str) -> None:
        if self.schemes is not None and scheme not in self.schemes:
            raise UsageError(f"{type(self).__name__}({self.name!r}): root {root!r} is not {' or '.join(self.schemes)}")

    def attribute_names(self) -> tuple:
        return ATTRIBUTES

    def at(self, path: str) -> At:
        """A place under this storage, for `data=` and `temp=`; cleanup knows it from here on."""
        path = place_path(path, f"{self.name}.at({path!r})")
        # Not a field: a place is not part of what the storage is, so not of its identity.
        places = self.__dict__.setdefault("_places", [])
        if path not in places:
            places.append(path)
        return At(self, path)

    def temp_places(self) -> list:
        """Where a run may write under the root: `temp_path`, and every `.at()` path asked for."""
        return [self.temp_path, *(p for p in self.__dict__.get("_places", ()) if p != self.temp_path)]


def _local(uri):
    return local_path(uri) if scheme_of(uri) == "file" else uri


class Storage(Resource):
    """A storage with its root filled in. A child is owned: the run removes it, where the scheme
    has a way to remove things."""

    def __init__(self, definition, identity, run_token, inputs=None, *, parent=None, root=None):
        super().__init__(definition, identity, run_token, inputs)
        self.parent = parent
        self.root = (root or fill(definition.root, self.inputs)).rstrip("/")
        self.scheme = scheme_of(self.root)
        definition.check_scheme(self.scheme, self.root)
        endpoint = fill(definition.endpoint, self.inputs) if definition.endpoint else None
        account = fill(definition.account, self.inputs) if definition.account else None
        if account is None and self.scheme == "abfss" and "@" in self.root:
            account = self.root.split("@", 1)[1].split(".", 1)[0]
        source = f"storage {self.name}"
        facts = {
            "root": self.root,
            "scheme": self.scheme,
            "data_root": self.root,
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

    def run_root(self, run_token, at=None):
        """Where everything the run `run_token` makes in a place of this storage is: `temp_path`, or
        `at`, a path under the root."""
        return f"{self.root}/{at or self.definition.temp_path}/{run_token}"

    def _run_roots(self, run_token):
        return [self.run_root(run_token, place) for place in self.definition.temp_places()]

    def temp_dir(self, at=None, key=None):
        """`TEMP_DIR` in the place `at`: the run's level, or the child for `key` under it, as a body reads it."""
        run_level = self.run_root(self.run_token or RUN_TOKEN_PENDING, at)
        return _local(run_level if key is None else f"{run_level}/{key}")

    def data_dir(self, at=None):
        """`DATA_DIR` in the place `at`: the root, or `at` under it."""
        return _local(self.root if at is None else f"{self.root}/{at}")

    def make_place(self, place):
        """Make a test's own place; an object store has no directories to make."""
        if scheme_of(place) == "file":
            os.makedirs(local_path(place), exist_ok=True)

    def mint_child(self, key):
        if self.run_token is None:
            raise ValueError(f"storage {self.name}: a child needs the run's run_token")
        root = f"{self.run_root(self.run_token)}/{key}"
        return Storage(
            self.definition, digest([self.identity, key]), self.run_token, self.inputs, parent=self, root=root
        )

    def remote(self):
        """The operations on the store of an s3 or az root, from the service it is on; None when it is on
        none, and so the run cannot remove what it wrote there."""
        if self.scheme not in ("s3", "az"):
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
        # A declared root is not made: a test's own place is (`make_place`), and the binary makes a `.test` body's.
        if self.scheme == "file" and self.owned:
            os.makedirs(self.path, exist_ok=True)

    def dealloc(self):
        if self.owned:  # the root was declared, not made: it is never removed
            self._remove(self.root)

    def sweep(self, run_token):
        """Remove what the run `run_token` left in the places of this storage; returns what `list` found."""
        found = self.list(run_token)
        for under in self._run_roots(run_token):
            if any(uri.startswith(under + "/") for uri in found):
                self._remove(under)
        return found

    def clear(self, run_token, keep=()):
        """At the end of a run: remove what the run `run_token` left in the places of this storage but
        the places in `keep` and what holds them, then the run's own directory and the place, where
        they are empty; an object store has no directories to remove. Returns what it removed."""
        removed = []
        for under in self._run_roots(run_token):
            removed += self._clear_one(under, keep)
        return removed

    def _clear_one(self, under, keep):
        if self.scheme != "file":
            doomed = [f"{under}/{n}" for n in self._names_under(under)]
            doomed = [uri for uri in doomed if not any(k == uri or k.startswith(uri + "/") for k in keep)]
            for uri in doomed:
                self._remove(uri)
            return doomed
        under = local_path(under)
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
        return {"name": self.name, "root": self.root}

    def ready(self):
        return self.scheme != "file" or os.path.isdir(self.path)

    def list(self, run_token):
        return [f"{under}/{n}" for under in self._run_roots(run_token) for n in self._names_under(under)]

    def delivery(self):
        """Its values, which `storage=` gives a test; the roots are the places `data=` and `temp=` bind."""
        facts = {name: a.value for name, a in self.attributes.items()}
        out = {}
        if self.scheme == "s3" and facts["endpoint"]:
            out["S3_ENDPOINT"] = facts["endpoint"].split("://", 1)[-1]
            out["S3_USE_SSL"] = "true" if facts["endpoint"].startswith("https://") else "false"
        if self.scheme in ("az", "abfss") and facts["account"]:
            out["AZ_ACCOUNT"] = facts["account"]
        source = f"storage {self.name}"
        return {name: Attribute(value, source) for name, value in out.items()}, {}


StorageDef.instance = Storage


@dataclass(eq=False)
class LocalStorageDef(StorageDef):
    """A local directory: `root` is a path, `file:///path` or `file://localhost/path`."""

    schemes = ("file",)


@dataclass(eq=False)
class S3StorageDef(StorageDef):
    """A place on a store that speaks S3, `s3://<bucket>/<path>`; `endpoint` is the server's, where it
    is not AWS. `S3ServiceDef.storage(...)` declares one on a server."""

    schemes = ("s3",)


@dataclass(eq=False)
class AzureStorageDef(StorageDef):
    """A place on Azure blob storage, `az://<container>/<path>` or `abfss://...`; `account` is the
    storage account, where the root does not name it. `AzureServiceDef.storage(...)` declares one on a
    server."""

    schemes = ("az", "abfss")


@dataclass(eq=False)
class DefaultStorageDef(StorageDef):
    """The run's default storage, declared: a project file may name it in `storages=` to say what its
    tests use. Binding it is binding nothing: the test gets the default storage's roots."""

    root: str = "(the run's default storage)"


def default_storage(name="default") -> DefaultStorageDef:
    """The storage of a slot that binds nothing: `DATA_DIR` is `data/` at the top of the repository,
    and the test's `TEMP_DIR` is its own place in the run directory."""
    return DefaultStorageDef(name)


DEFAULT_TEMP_ROOT = "ducktest_temp"


@dataclass(frozen=True)
class DefaultStorage:
    """The storage of a `data=` or `temp=` that binds nothing, one for the run: fixtures are read from `data_dir`, and
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
