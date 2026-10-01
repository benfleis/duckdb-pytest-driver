"""ducktest5's operations on an S3 store: by a fake rclone on PATH, which records how it was called
and serves a canned listing, and by boto3 against a fake client."""

import json
import os
import sys

import pytest

from ducktest5.resources import remote
from ducktest5.resources.remote import Boto3Remote, RcloneRemote, S3Access, remote_for

ACCESS = S3Access("http://store:9000", "us-east-1", lambda: ("key-id-5d1", "secret-8e2"))

FAKE_RCLONE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_RCLONE_LOG"], "a") as log:
    config = {{k: v for k, v in os.environ.items() if k.startswith("RCLONE_CONFIG_")}}
    log.write(json.dumps({{"argv": sys.argv[1:], "config": config}}) + "\\n")
sys.stdout.write(os.environ.get("FAKE_RCLONE_OUT", ""))
sys.stderr.write(os.environ.get("FAKE_RCLONE_ERR", ""))
sys.exit(int(os.environ.get("FAKE_RCLONE_EXIT", "0")))
"""


@pytest.fixture
def rclone(tmp_path, monkeypatch):
    """A fake rclone first on PATH; returns the calls it saw."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "rclone"
    script.write_text(FAKE_RCLONE.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "rclone.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


def _command(call):
    """The rclone command without its bounds: the verb and its operands."""
    argv = call["argv"]
    return argv[argv.index("--low-level-retries") + 2 :]


def test_rclone_is_picked_when_it_is_on_path(rclone, monkeypatch):
    assert isinstance(remote_for(ACCESS), RcloneRemote)
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    assert isinstance(remote_for(ACCESS), Boto3Remote)


def test_rclone_gets_its_remote_from_the_environment_and_never_a_file(rclone, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_OUT", "k1/\nk2/\nstray.csv\n")
    assert remote_for(ACCESS).list("s3://ducktest/lake/temp/run1") == ["k1", "k2", "stray.csv"]
    (call,) = rclone()
    assert call["argv"][:2] == ["--config", "/dev/null"]
    assert _command(call) == ["lsf", "ducktest:ducktest/lake/temp/run1"]
    assert call["config"] == {
        "RCLONE_CONFIG_DUCKTEST_TYPE": "s3",
        "RCLONE_CONFIG_DUCKTEST_PROVIDER": "Other",
        "RCLONE_CONFIG_DUCKTEST_ENDPOINT": "http://store:9000",
        "RCLONE_CONFIG_DUCKTEST_REGION": "us-east-1",
        "RCLONE_CONFIG_DUCKTEST_ACCESS_KEY_ID": "key-id-5d1",
        "RCLONE_CONFIG_DUCKTEST_SECRET_ACCESS_KEY": "secret-8e2",
        "RCLONE_CONFIG_DUCKTEST_FORCE_PATH_STYLE": "true",
    }
    assert not any("secret-8e2" in a or "key-id-5d1" in a for a in call["argv"])


def test_rclone_deletes_a_prefix_and_makes_a_bucket(rclone):
    store = remote_for(ACCESS)
    store.delete("s3://ducktest/lake/temp/run1/k1")
    store.make_bucket("ducktest")
    assert [_command(c) for c in rclone()] == [
        ["delete", "ducktest:ducktest/lake/temp/run1/k1"],
        ["mkdir", "ducktest:ducktest"],
    ]


@pytest.mark.parametrize("code", [3, 4])
def test_rclone_finding_nothing_there_is_not_an_error(rclone, monkeypatch, code):
    monkeypatch.setenv("FAKE_RCLONE_EXIT", str(code))
    store = remote_for(ACCESS)
    assert store.list("s3://ducktest/gone") == []
    store.delete("s3://ducktest/gone")


def test_an_rclone_failure_says_what_rclone_said(rclone, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_EXIT", "1")
    monkeypatch.setenv("FAKE_RCLONE_ERR", "Failed to lsf: AccessDenied\n")
    with pytest.raises(RuntimeError, match=r"rclone lsf failed \(exit 1\): Failed to lsf: AccessDenied"):
        remote_for(ACCESS).list("s3://ducktest/lake")


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    """Keys of buckets; pages of `page_size` keys, as list_objects_v2 gives them."""

    def __init__(self, buckets, page_size=2, create_error=None):
        self.buckets, self.page_size, self.create_error = buckets, page_size, create_error
        self.deletes = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix="", Delimiter=None):  # noqa: N803
        if Bucket not in self.buckets:
            raise FakeClientError("NoSuchBucket")
        keys = sorted(k for k in self.buckets[Bucket] if k.startswith(Prefix))
        prefixes = []
        if Delimiter:
            rest = [(k, k[len(Prefix) :]) for k in keys]
            prefixes = sorted({Prefix + r.split(Delimiter, 1)[0] + Delimiter for _, r in rest if Delimiter in r})
            keys = [k for k, r in rest if Delimiter not in r]
        for start in range(0, max(len(keys), 1), self.page_size):
            page = {"Contents": [{"Key": k} for k in keys[start : start + self.page_size]]}
            if start == 0 and prefixes:
                page["CommonPrefixes"] = [{"Prefix": p} for p in prefixes]
            yield page

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        self.deletes.append(len(Delete["Objects"]))
        for o in Delete["Objects"]:
            self.buckets[Bucket].remove(o["Key"])

    def create_bucket(self, Bucket):  # noqa: N803
        if self.create_error:
            raise FakeClientError(self.create_error)
        self.buckets.setdefault(Bucket, set())


@pytest.fixture
def boto3_remote(monkeypatch):
    client = FakeS3Client(
        {
            "ducktest": {
                "lake/temp/run1/k1/a.csv",
                "lake/temp/run1/k1/b/c.csv",
                "lake/temp/run1/k10/a.csv",
                "lake/temp/run1/stray.csv",
                "lake/temp/run2/k1/a.csv",
            }
        }
    )
    store = Boto3Remote(ACCESS)
    monkeypatch.setattr(store, "_client", lambda: client)
    return store, client


def test_boto3_lists_what_is_directly_under_a_prefix(boto3_remote):
    store, _ = boto3_remote
    assert sorted(store.list("s3://ducktest/lake/temp/run1")) == ["k1", "k10", "stray.csv"]
    assert store.list("s3://ducktest/lake/temp/run9") == []
    assert store.list("s3://nobucket/lake") == []


def test_boto3_deletes_a_prefix_and_no_neighbour_that_shares_its_start(boto3_remote):
    store, client = boto3_remote
    store.delete("s3://ducktest/lake/temp/run1/k1")
    assert sorted(client.buckets["ducktest"]) == [
        "lake/temp/run1/k10/a.csv",
        "lake/temp/run1/stray.csv",
        "lake/temp/run2/k1/a.csv",
    ]
    store.delete("s3://ducktest/lake/temp/run1/k1")


def test_boto3_deletes_in_batches_of_a_thousand(monkeypatch):
    client = FakeS3Client({"b": {f"p/{n:04}" for n in range(2500)}}, page_size=1000)
    store = Boto3Remote(ACCESS)
    monkeypatch.setattr(store, "_client", lambda: client)
    store.delete("s3://b/p")
    assert client.deletes == [1000, 1000, 500] and client.buckets["b"] == set()


@pytest.mark.parametrize("code", ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_boto3_making_a_bucket_that_is_there_is_fine(monkeypatch, code):
    store = Boto3Remote(ACCESS)
    monkeypatch.setattr(store, "_client", lambda: FakeS3Client({}, create_error=code))
    store.make_bucket("ducktest")


def test_boto3_does_not_hide_another_error_of_a_bucket(monkeypatch):
    store = Boto3Remote(ACCESS)
    monkeypatch.setattr(store, "_client", lambda: FakeS3Client({}, create_error="AccessDenied"))
    with pytest.raises(FakeClientError, match="AccessDenied"):
        store.make_bucket("ducktest")


# -----------------------------------------------------------------------------
# Loading data in, checking it, and whole buckets
#


@pytest.fixture
def dat(tmp_path):
    root = tmp_path / "dat"
    (root / "t1").mkdir(parents=True)
    (root / "t1" / "a.parquet").write_bytes(b"one")
    (root / "b.json").write_bytes(b"{}")
    return str(root)


def test_rclone_copies_syncs_and_removes_a_bucket(rclone, dat):
    store = remote_for(ACCESS)
    store.copy(dat, "s3://test-bucket/dat")
    store.sync(dat, "s3://test-bucket/dat")
    store.make_bucket("test-bucket")
    store.delete_bucket("test-bucket")
    assert [_command(c) for c in rclone()] == [
        ["copy", dat, "ducktest:test-bucket/dat"],
        ["sync", dat, "ducktest:test-bucket/dat"],
        ["mkdir", "ducktest:test-bucket"],
        ["purge", "ducktest:test-bucket"],
    ]


@pytest.mark.parametrize("code, same", [(0, True), (1, False), (3, False)])
def test_rclone_check_says_whether_the_store_holds_the_directory(rclone, monkeypatch, dat, code, same):
    monkeypatch.setenv("FAKE_RCLONE_EXIT", str(code))
    assert remote_for(ACCESS).check(dat, "s3://test-bucket/dat") is same
    (call,) = rclone()
    assert _command(call) == ["check", "--one-way", dat, "ducktest:test-bucket/dat"]


def test_an_rclone_check_that_fails_is_not_a_difference(rclone, monkeypatch, dat):
    monkeypatch.setenv("FAKE_RCLONE_EXIT", "7")
    with pytest.raises(RuntimeError, match=r"rclone check failed \(exit 7\)"):
        remote_for(ACCESS).check(dat, "s3://test-bucket/dat")


def test_rclone_finds_a_bucket_among_the_buckets(rclone, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_OUT", "ducktest/\ntest-bucket/\n")
    store = remote_for(ACCESS)
    assert store.bucket_exists("test-bucket") and not store.bucket_exists("test")
    assert _command(rclone()[0]) == ["lsf", "ducktest:"]


def test_rclone_opens_a_bucket_by_a_policy_through_boto3(rclone, monkeypatch):
    client = FakeObjectClient({"pub": {}})
    monkeypatch.setattr(Boto3Remote, "_client", lambda self: client)
    remote_for(ACCESS).make_public("pub")
    (statement,) = json.loads(client.policies["pub"])["Statement"]
    assert statement["Principal"] == {"AWS": ["*"]} and statement["Action"] == ["s3:GetObject", "s3:ListBucket"]
    assert statement["Resource"] == ["arn:aws:s3:::pub", "arn:aws:s3:::pub/*"]
    assert rclone() == []


def test_a_bucket_is_public_when_it_lists_without_keys(monkeypatch):
    asked = []

    def urlopen(url, timeout):
        asked.append(url)
        raise remote.urllib.error.HTTPError(url, 403, "denied", {}, None)

    monkeypatch.setattr(remote.urllib.request, "urlopen", urlopen)
    assert not Boto3Remote(ACCESS).is_public("pub")
    assert asked == ["http://store:9000/pub?list-type=2&max-keys=1"]


class FakeObjectClient:
    """Buckets of {key: bytes}, with sizes and ETags as S3 gives them."""

    def __init__(self, buckets):
        self.buckets, self.policies = buckets, {}

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix="", Delimiter=None):  # noqa: N803
        if Bucket not in self.buckets:
            raise FakeClientError("NoSuchBucket")
        found = sorted(k for k in self.buckets[Bucket] if k.startswith(Prefix))
        yield {
            "Contents": [
                {
                    "Key": k,
                    "Size": len(self.buckets[Bucket][k]),
                    "ETag": f'"{remote.hashlib.md5(self.buckets[Bucket][k]).hexdigest()}"',
                }
                for k in found
            ]
        }

    def upload_file(self, path, bucket, key):
        with open(path, "rb") as f:
            self.buckets[bucket][key] = f.read()

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for o in Delete["Objects"]:
            del self.buckets[Bucket][o["Key"]]

    def list_buckets(self):
        return {"Buckets": [{"Name": b} for b in self.buckets]}

    def delete_bucket(self, Bucket):  # noqa: N803
        if Bucket not in self.buckets:
            raise FakeClientError("NoSuchBucket")
        assert not self.buckets[Bucket], "a bucket is emptied before it is removed"
        del self.buckets[Bucket]

    def put_bucket_policy(self, Bucket, Policy):  # noqa: N803
        self.policies[Bucket] = Policy


@pytest.fixture
def objects(monkeypatch):
    client = FakeObjectClient({"test-bucket": {"keep.csv": b"theirs"}})
    store = Boto3Remote(ACCESS)
    monkeypatch.setattr(store, "_client", lambda: client)
    return store, client


def test_boto3_copies_a_directory_and_checks_it(objects, dat):
    store, client = objects
    assert not store.check(dat, "s3://test-bucket/dat")
    store.copy(dat, "s3://test-bucket/dat")
    assert client.buckets["test-bucket"] == {"keep.csv": b"theirs", "dat/t1/a.parquet": b"one", "dat/b.json": b"{}"}
    assert store.check(dat, "s3://test-bucket/dat")
    client.buckets["test-bucket"]["dat/b.json"] = b"[]"
    assert not store.check(dat, "s3://test-bucket/dat"), "same size, other content"


def test_boto3_sync_removes_what_the_directory_does_not_have(objects, dat):
    store, client = objects
    client.buckets["test-bucket"]["dat/old.json"] = b"x"
    store.sync(dat, "s3://test-bucket/dat")
    assert sorted(client.buckets["test-bucket"]) == ["dat/b.json", "dat/t1/a.parquet", "keep.csv"]


def test_boto3_removes_a_whole_bucket(objects):
    store, client = objects
    assert store.bucket_exists("test-bucket") and not store.bucket_exists("other")
    store.delete_bucket("test-bucket")
    store.delete_bucket("test-bucket")
    assert client.buckets == {}
