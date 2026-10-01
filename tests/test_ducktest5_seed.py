"""Seeds: buckets with content on a store, made before the tests that bind them, against a fake store."""

import os

import pytest

from ducktest5.resources import Service, ServiceNotUp, check_contract
from ducktest5.resources.made import EXISTING, FOUND, MANAGED, Made, services_in
from ducktest5.resources.seed import SeedDef
from ducktest5.services import s3local
from ducktest5.services.s3local import s3_local


class FakeAccess:
    scheme = "s3"


class FakeStore:
    """Buckets of {key: bytes}, and which are public. `fail_on` names a verb that raises."""

    access = FakeAccess()

    def __init__(self, buckets=None, public=(), fail_on=None):
        self.buckets = {b: dict(keys) for b, keys in (buckets or {}).items()}
        self.public, self.fail_on, self.calls = set(public), fail_on, []

    def _verb(self, name, *args):
        self.calls.append((name, *args))
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")

    @staticmethod
    def _split(uri):
        bucket, _, key = uri.split("://", 1)[1].partition("/")
        return bucket, f"{key}/" if key else ""

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self._verb("make_bucket", bucket)
        self.buckets.setdefault(bucket, {})

    def copy(self, source, uri):
        self._verb("copy", source, uri)
        bucket, prefix = self._split(uri)
        for relative, data in _files(source).items():
            self.buckets[bucket][prefix + relative] = data

    def check(self, source, uri):
        bucket, prefix = self._split(uri)
        there = self.buckets.get(bucket, {})
        return all(there.get(prefix + k) == v for k, v in _files(source).items())

    def make_public(self, bucket):
        self._verb("make_public", bucket)
        self.public.add(bucket)

    def is_public(self, bucket):
        return bucket in self.public

    def delete_bucket(self, bucket):
        self._verb("delete_bucket", bucket)
        self.buckets.pop(bucket, None)
        self.public.discard(bucket)


def _files(source):
    out = {}
    for dirpath, _, names in os.walk(source):
        for name in names:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, source)] = f.read()
    return out


@pytest.fixture
def dat(tmp_path):
    """A local directory of two tables."""
    root = tmp_path / "dat"
    for table in ("t1", "t2"):
        (root / table).mkdir(parents=True)
        (root / table / "part-0.parquet").write_bytes(table.encode())
    return str(root)


@pytest.fixture
def server(monkeypatch):
    """A running local S3 server whose store is the fake; returns (its Def, the store)."""
    store = FakeStore()
    monkeypatch.setattr(s3local, "s3_answers", lambda endpoint: True)
    monkeypatch.setattr(s3local.S3Local, "remote", lambda self: store)
    monkeypatch.setattr(s3local.S3Local, "create", Service.create)  # its own bucket is not what these test
    return s3_local(), store


def _seed(server_def, seed_def):
    made = Made("r1")
    made.create(server_def)
    return made, made.create(seed_def)


def test_a_seed_keeps_the_contract(server, dat):
    s3, _ = server
    made = Made("contract-run")
    made.create(s3)
    seed = s3.seed("dat", "test-bucket", copy=[(dat, "dat")])
    check_contract(lambda run_token: seed.make(made.inputs_of(seed), run_token=run_token))


def test_a_seed_makes_its_bucket_fills_it_and_removes_it_at_the_end(server, dat):
    s3, store = server
    made, seed = _seed(s3, s3.seed("dat", "test-bucket", copy=[(dat, "dat")], public=True))
    assert made.modes["dat"] == MANAGED and seed.owned
    assert sorted(store.buckets["test-bucket"]) == ["dat/t1/part-0.parquet", "dat/t2/part-0.parquet"]
    assert store.public == {"test-bucket"}
    assert seed.attribute_values() == {"uri": "s3://test-bucket"}
    assert seed.list("r1") == ["s3://test-bucket"]
    made.end(failed=False)
    assert "test-bucket" not in store.buckets and store.public == set()


def test_a_bucket_that_holds_the_seed_already_is_used_as_it_is_and_left(server, dat):
    s3, store = server
    store.buckets["test-bucket"] = {"dat/" + k: v for k, v in _files(dat).items()}
    store.buckets["test-bucket"]["theirs.csv"] = b"x"
    store.public.add("test-bucket")
    made, seed = _seed(s3, s3.seed("dat", "test-bucket", copy=[(dat, "dat")], public=True))
    assert made.modes["dat"] == FOUND and not seed.owned
    assert store.calls == []
    made.end(failed=True)
    assert "theirs.csv" in store.buckets["test-bucket"] and store.calls == []


@pytest.mark.parametrize("what", ["content", "public"])
def test_a_bucket_that_is_there_with_other_content_is_refused_and_not_touched(server, dat, what):
    s3, store = server
    store.buckets["test-bucket"] = {"dat/" + k: v for k, v in _files(dat).items()}
    if what == "content":
        store.buckets["test-bucket"]["dat/t1/part-0.parquet"] = b"changed"
    public = what == "public"
    made = Made("r1")
    made.create(s3)
    with pytest.raises(ServiceNotUp, match="does not change a bucket it did not make"):
        made.create(s3.seed("dat", "test-bucket", copy=[(dat, "dat")], public=public))
    assert "dat" in made.errors and store.calls == []


def test_a_seed_that_fails_half_way_removes_the_bucket_it_made(server, dat):
    s3, store = server
    store.fail_on = "make_public"
    made = Made("r1")
    made.create(s3)
    with pytest.raises(ServiceNotUp, match="make_public failed"):
        made.create(s3.seed("dat", "test-bucket", copy=[(dat, "dat")], public=True))
    assert "test-bucket" not in store.buckets


def test_a_seed_whose_directory_is_not_there_is_not_up_and_makes_nothing(server, tmp_path):
    s3, store = server
    made = Made("r1")
    made.create(s3)
    with pytest.raises(ServiceNotUp, match="no directory at"):
        made.create(s3.seed("dat", "test-bucket", copy=[(str(tmp_path / "gone"), "dat")]))
    assert store.calls == []


@pytest.mark.parametrize("retain, kept", [("never", False), ("on_failure", True), ("always", True)])
def test_retain_keeps_a_made_bucket_after_a_failed_run(server, dat, retain, kept):
    s3, store = server
    made, _ = _seed(s3, s3.seed("dat", "test-bucket", copy=[(dat, "dat")], retain=retain))
    made.end(failed=True)
    assert ("test-bucket" in store.buckets) is kept


def test_an_existing_seed_is_checked_never_made_and_never_removed(server, dat):
    s3, store = server
    seed = s3.seed("dat", "test-bucket", copy=[(dat, "dat")])
    made = Made("r1")
    made.create(s3)
    with pytest.raises(ServiceNotUp, match="there is no bucket"):
        made.create(seed.as_existing("s3://test-bucket"), EXISTING)
    store.buckets["test-bucket"] = {"dat/" + k: v for k, v in _files(dat).items()}
    made.create(seed.as_existing("s3://test-bucket"), EXISTING)
    made.end(failed=False)
    assert "test-bucket" in store.buckets and store.calls == []


def test_an_empty_seed_is_an_extra_bucket(server):
    s3, store = server
    made, _ = _seed(s3, s3.seed("scratch", "scratch-bucket"))
    assert store.buckets == {"scratch-bucket": {}}
    made.end(failed=False)
    assert store.buckets == {}


def test_a_seed_is_made_after_its_server(server, dat):
    s3, _ = server
    seed = s3.seed("dat", "test-bucket", copy=[(dat, "dat")])
    assert [d.id for d in services_in([seed])] == ["s3", "dat"]
    assert isinstance(seed, SeedDef) and seed.inputs == [s3]
