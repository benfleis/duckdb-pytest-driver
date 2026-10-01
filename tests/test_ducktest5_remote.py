"""ducktest5's file operations on a store: by a fake rclone on PATH, which records how it was called
and serves a canned listing."""

import json
import os
import sys

import pytest

from ducktest5.resources import remote
from ducktest5.resources.remote import S3Access, remote_for

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


def test_without_rclone_an_operation_says_to_install_it_and_making_the_remote_does_not(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    store = remote_for(ACCESS)
    with pytest.raises(RuntimeError, match=r"rclone is not on PATH.*install it"):
        store.list("s3://ducktest/lake")


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


def test_rclone_deletes_a_prefix(rclone):
    remote_for(ACCESS).delete("s3://ducktest/lake/temp/run1/k1")
    assert [_command(c) for c in rclone()] == [["delete", "ducktest:ducktest/lake/temp/run1/k1"]]


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


def test_rclone_copies_and_syncs_a_directory_in(rclone, dat):
    store = remote_for(ACCESS)
    store.copy(dat, "s3://test-bucket/dat")
    store.sync(dat, "s3://test-bucket/dat")
    assert [_command(c) for c in rclone()] == [
        ["copy", dat, "ducktest:test-bucket/dat"],
        ["sync", dat, "ducktest:test-bucket/dat"],
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
