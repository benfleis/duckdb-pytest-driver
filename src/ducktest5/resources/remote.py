"""What the run does to an S3-compatible store itself: list what is directly under a prefix, delete a
prefix, make a bucket. rclone does it when it is on PATH, else boto3 (the `s3` extra).

The keys are read when an operation runs and reach rclone as its environment, never a file or its
command line."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Protocol

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Access:
    """How to reach one store: `endpoint` is http(s)://host:port; `keys()` gives (key id, secret)."""

    endpoint: str
    region: str
    keys: Callable[[], tuple[str, str]]


class Remote(Protocol):
    def list(self, uri: str) -> list[str]:
        """The names directly under `uri` (`s3://bucket/prefix`), objects and prefixes alike."""

    def delete(self, uri: str) -> None:
        """Everything under `uri`; nothing there is not an error."""

    def make_bucket(self, bucket: str) -> None:
        """Make `bucket`; one that is there is not an error."""


def split(uri: str) -> tuple[str, str]:
    """(bucket, key) of `s3://bucket/key`."""
    bucket, _, key = uri.split("://", 1)[1].partition("/")
    return bucket, key.strip("/")


def remote_for(access: S3Access) -> Remote:
    rclone = shutil.which("rclone")
    return RcloneRemote(access, rclone) if rclone else Boto3Remote(access)


class RcloneRemote:
    """rclone with a remote of its own named by `RCLONE_CONFIG_DUCKTEST_*`, and no config file."""

    name = "ducktest"
    _NOT_FOUND = (3, 4)  # rclone's exit codes for a directory, a file, that is not there
    _BOUNDS = ("--contimeout", "5s", "--timeout", "30s", "--retries", "1", "--low-level-retries", "1")

    def __init__(self, access: S3Access, rclone: str = "rclone"):
        self.access, self.rclone = access, rclone

    def _env(self):
        key_id, secret = self.access.keys()
        conf = {
            "TYPE": "s3",
            "PROVIDER": "Other",
            "ENDPOINT": self.access.endpoint,
            "REGION": self.access.region,
            "ACCESS_KEY_ID": key_id,
            "SECRET_ACCESS_KEY": secret,
            "FORCE_PATH_STYLE": "true",
        }
        prefix = f"RCLONE_CONFIG_{self.name.upper()}_"
        return {**os.environ, **{prefix + k: v for k, v in conf.items()}}

    def _run(self, *args) -> str | None:
        """rclone's output, or None when what `args` names is not there."""
        # /dev/null keeps a user's own rclone.conf, and a remote of the same name in it, out of this.
        cmd = [self.rclone, "--config", "/dev/null", *self._BOUNDS, *args]
        _log.debug("rclone: %s", shlex.join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, env=self._env(), timeout=120)
        if proc.returncode in self._NOT_FOUND:
            return None
        if proc.returncode != 0:
            tail = " | ".join((proc.stderr or proc.stdout).strip().splitlines()[-3:])
            raise RuntimeError(f"rclone {args[0]} failed (exit {proc.returncode}): {tail}")
        return proc.stdout

    def _path(self, uri):
        bucket, key = split(uri)
        return f"{self.name}:{bucket}/{key}" if key else f"{self.name}:{bucket}"

    def list(self, uri):
        out = self._run("lsf", self._path(uri))
        return [line.rstrip("/") for line in (out or "").splitlines() if line]

    def delete(self, uri):
        self._run("delete", self._path(uri))

    def make_bucket(self, bucket):
        self._run("mkdir", f"{self.name}:{bucket}")


class Boto3Remote:
    def __init__(self, access: S3Access):
        self.access = access

    def _client(self):
        import boto3  # the [s3] extra; imported here so a run that has rclone, or no s3, needs no boto3
        from botocore.config import Config

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

    def list(self, uri):
        bucket, key = split(uri)
        prefix = f"{key}/" if key else ""
        names = []
        for page in self._pages(self._client(), Bucket=bucket, Prefix=prefix, Delimiter="/"):
            names += [p["Prefix"][len(prefix) :].rstrip("/") for p in page.get("CommonPrefixes", [])]
            names += [o["Key"][len(prefix) :] for o in page.get("Contents", [])]
        return names

    def delete(self, uri):
        bucket, key = split(uri)
        client = self._client()
        found = [
            o["Key"]
            for page in self._pages(client, Bucket=bucket, Prefix=key)
            for o in page.get("Contents", [])
            if not key or o["Key"] == key or o["Key"].startswith(key + "/")
        ]
        for start in range(0, len(found), 1000):  # the most one request deletes
            batch = [{"Key": k} for k in found[start : start + 1000]]
            client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})

    def make_bucket(self, bucket):
        try:
            self._client().create_bucket(Bucket=bucket)
        except Exception as exc:
            # Make and see, not look and then make: two runs on one store would race.
            if _code(exc) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise


def _code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code")
