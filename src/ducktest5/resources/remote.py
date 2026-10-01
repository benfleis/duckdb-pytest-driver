"""What the run does to an object store itself: list, delete, copy and sync a local directory in,
check that one is there, make and remove a bucket, open a bucket to anonymous reads. rclone does it
when it is on PATH, else boto3 (the `s3` extra), for S3.

The keys are read when an operation runs and reach rclone as its environment, never a file or its
command line. An `Access` says how to reach one store; a store of another kind (Azure blob) is
another `Access` with its own rclone backend, and the verbs stay the same."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Access:
    """How to reach one store: `endpoint` is http(s)://host:port; `keys()` gives (key id, secret)."""

    endpoint: str
    region: str
    keys: Callable[[], tuple[str, str]]

    scheme = "s3"

    def rclone_config(self) -> dict:
        key_id, secret = self.keys()
        return {
            "TYPE": "s3",
            "PROVIDER": "Other",
            "ENDPOINT": self.endpoint,
            "REGION": self.region,
            "ACCESS_KEY_ID": key_id,
            "SECRET_ACCESS_KEY": secret,
            "FORCE_PATH_STYLE": "true",
        }


class Remote(Protocol):
    def list(self, uri: str) -> list[str]:
        """The names directly under `uri` (`s3://bucket/prefix`), objects and prefixes alike."""

    def delete(self, uri: str) -> None:
        """Everything under `uri`; nothing there is not an error."""

    def copy(self, source: str, uri: str) -> None:
        """Every file under the local directory `source` to the same path under `uri`."""

    def sync(self, source: str, uri: str) -> None:
        """`copy`, and what is under `uri` and not under `source` goes."""

    def check(self, source: str, uri: str) -> bool:
        """Whether every file under `source` is under `uri` with the same content; more there is fine."""

    def make_bucket(self, bucket: str) -> None:
        """Make `bucket`; one that is there is not an error."""

    def bucket_exists(self, bucket: str) -> bool: ...

    def delete_bucket(self, bucket: str) -> None:
        """`bucket` and everything in it; one that is not there is not an error."""

    def make_public(self, bucket: str) -> None:
        """Anyone may list `bucket` and read its objects, without keys."""

    def is_public(self, bucket: str) -> bool: ...


def split(uri: str) -> tuple[str, str]:
    """(bucket, key) of `s3://bucket/key`."""
    bucket, _, key = uri.split("://", 1)[1].partition("/")
    return bucket, key.strip("/")


def remote_for(access: S3Access) -> Remote:
    rclone = shutil.which("rclone")
    return RcloneRemote(access, rclone) if rclone else Boto3Remote(access)


def public_read_policy(bucket: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                }
            ],
        }
    )


def anonymous_list(access: S3Access, bucket: str) -> bool:
    """Whether the store lists `bucket` for a request with no keys."""
    try:
        with urllib.request.urlopen(f"{access.endpoint}/{bucket}?list-type=2&max-keys=1", timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


class RcloneRemote:
    """rclone with a remote of its own named by `RCLONE_CONFIG_DUCKTEST_*`, and no config file."""

    name = "ducktest"
    _NOT_FOUND = (3, 4)  # rclone's exit codes for a directory, a file, that is not there
    _BOUNDS = ("--contimeout", "5s", "--timeout", "30s", "--retries", "1", "--low-level-retries", "1")

    def __init__(self, access: S3Access, rclone: str = "rclone"):
        self.access, self.rclone = access, rclone

    def _env(self):
        prefix = f"RCLONE_CONFIG_{self.name.upper()}_"
        return {**os.environ, **{prefix + k: v for k, v in self.access.rclone_config().items()}}

    def _call(self, *args) -> subprocess.CompletedProcess:
        # /dev/null keeps a user's own rclone.conf, and a remote of the same name in it, out of this.
        cmd = [self.rclone, "--config", "/dev/null", *self._BOUNDS, *args]
        _log.debug("rclone: %s", shlex.join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, env=self._env(), timeout=600)

    def _run(self, *args) -> str | None:
        """rclone's output, or None when what `args` names is not there."""
        proc = self._call(*args)
        if proc.returncode in self._NOT_FOUND:
            return None
        if proc.returncode != 0:
            raise RuntimeError(f"rclone {args[0]} failed (exit {proc.returncode}): {_tail(proc)}")
        return proc.stdout

    def _path(self, uri):
        bucket, key = split(uri)
        return f"{self.name}:{bucket}/{key}" if key else f"{self.name}:{bucket}"

    def list(self, uri):
        out = self._run("lsf", self._path(uri))
        return [line.rstrip("/") for line in (out or "").splitlines() if line]

    def delete(self, uri):
        self._run("delete", self._path(uri))

    def copy(self, source, uri):
        self._run("copy", source, self._path(uri))

    def sync(self, source, uri):
        self._run("sync", source, self._path(uri))

    def check(self, source, uri):
        proc = self._call("check", "--one-way", source, self._path(uri))
        if proc.returncode in (0, 1, *self._NOT_FOUND):  # 1: differences found
            return proc.returncode == 0
        raise RuntimeError(f"rclone check failed (exit {proc.returncode}): {_tail(proc)}")

    def make_bucket(self, bucket):
        self._run("mkdir", f"{self.name}:{bucket}")

    def bucket_exists(self, bucket):
        return bucket in self.list(f"{self.access.scheme}://")

    def delete_bucket(self, bucket):
        self._run("purge", f"{self.name}:{bucket}")

    def make_public(self, bucket):
        # rclone has no verb for a bucket policy, and RustFS ignores a canned bucket ACL.
        Boto3Remote(self.access).make_public(bucket)

    def is_public(self, bucket):
        return anonymous_list(self.access, bucket)


def _tail(proc):
    return " | ".join((proc.stderr or proc.stdout).strip().splitlines()[-3:])


def _files(source):
    """(key relative to `source`, path) of every file under the local directory `source`."""
    for dirpath, _, filenames in os.walk(source):
        for name in filenames:
            path = os.path.join(dirpath, name)
            yield os.path.relpath(path, source).replace(os.sep, "/"), path


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


class Boto3Remote:
    def __init__(self, access: S3Access):
        self.access = access

    def _client(self):
        try:
            import boto3  # the [s3] extra; imported here so a run that has rclone, or no s3, needs no boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("this needs boto3: install ducktest with the `s3` extra, or put rclone on PATH") from exc

        key_id, secret = self.access.keys()
        return boto3.client(
            "s3",
            endpoint_url=self.access.endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name=self.access.region,
            config=Config(s3={"addressing_style": "path"}, connect_timeout=5, retries={"max_attempts": 1}),
        )

    @staticmethod
    def _pages(client, **request):
        try:
            yield from client.get_paginator("list_objects_v2").paginate(**request)
        except Exception as exc:
            if _code(exc) != "NoSuchBucket":
                raise

    def _objects(self, client, uri):
        """{key relative to `uri`: object} of everything under `uri`."""
        bucket, key = split(uri)
        prefix = f"{key}/" if key else ""
        return {
            o["Key"][len(prefix) :]: o
            for page in self._pages(client, Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])
        }

    def list(self, uri):
        bucket, key = split(uri)
        prefix = f"{key}/" if key else ""
        names = []
        for page in self._pages(self._client(), Bucket=bucket, Prefix=prefix, Delimiter="/"):
            names += [p["Prefix"][len(prefix) :].rstrip("/") for p in page.get("CommonPrefixes", [])]
            names += [o["Key"][len(prefix) :] for o in page.get("Contents", [])]
        return names

    def _delete_keys(self, client, bucket, keys):
        for start in range(0, len(keys), 1000):  # the most one request deletes
            batch = [{"Key": k} for k in keys[start : start + 1000]]
            client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})

    def delete(self, uri):
        bucket, key = split(uri)
        client = self._client()
        found = [
            o["Key"]
            for page in self._pages(client, Bucket=bucket, Prefix=key)
            for o in page.get("Contents", [])
            if not key or o["Key"] == key or o["Key"].startswith(key + "/")
        ]
        self._delete_keys(client, bucket, found)

    def copy(self, source, uri):
        bucket, key = split(uri)
        client = self._client()
        for relative, path in _files(source):
            client.upload_file(path, bucket, f"{key}/{relative}" if key else relative)

    def sync(self, source, uri):
        self.copy(source, uri)
        bucket, key = split(uri)
        client = self._client()
        wanted = {relative for relative, _ in _files(source)}
        prefix = f"{key}/" if key else ""
        self._delete_keys(client, bucket, [prefix + k for k in self._objects(client, uri) if k not in wanted])

    def check(self, source, uri):
        there = self._objects(self._client(), uri)
        for relative, path in _files(source):
            o = there.get(relative)
            if o is None or o["Size"] != os.path.getsize(path):
                return False
            etag = o.get("ETag", "").strip('"')
            if "-" not in etag and etag != _md5(path):  # a multipart upload's ETag is not the content's md5
                return False
        return True

    def make_bucket(self, bucket):
        try:
            self._client().create_bucket(Bucket=bucket)
        except Exception as exc:
            # Make and see, not look and then make: two runs on one store would race.
            if _code(exc) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise

    def bucket_exists(self, bucket):
        return any(b["Name"] == bucket for b in self._client().list_buckets().get("Buckets", []))

    def delete_bucket(self, bucket):
        client = self._client()
        self._delete_keys(client, bucket, [o["Key"] for o in self._objects(client, f"s3://{bucket}").values()])
        try:
            client.delete_bucket(Bucket=bucket)
        except Exception as exc:
            if _code(exc) != "NoSuchBucket":
                raise

    def make_public(self, bucket):
        self._client().put_bucket_policy(Bucket=bucket, Policy=public_read_policy(bucket))

    def is_public(self, bucket):
        return anonymous_list(self.access, bucket)


def _code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code")
