"""What the run does to the files of a storage: list, delete a prefix, copy and sync a local directory
in, check that one is there. rclone does all of it, for every kind of store: its verbs read the same
for S3, Azure blob, GCS and R2, and only the backend it is given differs.

An `Access` says how to reach one store and gives rclone its backend (`rclone_config()`). The keys
are read when an operation runs and reach rclone as its environment, never a file or its command
line. A bucket itself (make, remove, open to reads) is the service's to manage, not the storage's."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

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


@dataclass(frozen=True)
class AzureBlobAccess:
    """How to reach one Azure blob account: `endpoint` is its blob service URL, which names the account
    on a local server (`http://host:10000/devstoreaccount1`); `key()` gives the account key."""

    endpoint: str
    account: str
    key: Callable[[], str]

    scheme = "az"

    def rclone_config(self) -> dict:
        return {"TYPE": "azureblob", "ACCOUNT": self.account, "KEY": self.key(), "ENDPOINT": self.endpoint}


def split(uri: str) -> tuple[str, str]:
    """(bucket, key) of `s3://bucket/key`, (container, key) of `az://container/key`."""
    bucket, _, key = uri.split("://", 1)[1].partition("/")
    return bucket, key.strip("/")


def remote_for(access) -> RcloneRemote:
    return RcloneRemote(access)


class RcloneRemote:
    """rclone with a remote of its own named by `RCLONE_CONFIG_DUCKTEST_*`, and no config file. It is
    looked for on PATH when an operation runs, so a run that needs none does not need rclone."""

    name = "ducktest"
    _NOT_FOUND = (3, 4)  # rclone's exit codes for a directory, a file, that is not there
    _BOUNDS = ("--contimeout", "5s", "--timeout", "30s", "--retries", "1", "--low-level-retries", "1")

    def __init__(self, access, rclone: str | None = None):
        self.access, self.rclone = access, rclone

    def _binary(self):
        found = self.rclone or shutil.which("rclone")
        if not found:
            raise RuntimeError(
                "rclone is not on PATH, and the run needs it for the files of a remote storage: "
                "install it (https://rclone.org/install/)"
            )
        return found

    def _env(self):
        prefix = f"RCLONE_CONFIG_{self.name.upper()}_"
        return {**os.environ, **{prefix + k: v for k, v in self.access.rclone_config().items()}}

    def _call(self, *args) -> subprocess.CompletedProcess:
        # /dev/null keeps a user's own rclone.conf, and a remote of the same name in it, out of this.
        cmd = [self._binary(), "--config", "/dev/null", *self._BOUNDS, *args]
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

    def list(self, uri) -> list[str]:
        """The names directly under `uri` (`s3://bucket/prefix`, `az://container/prefix`), objects and
        prefixes alike."""
        out = self._run("lsf", self._path(uri))
        return [line.rstrip("/") for line in (out or "").splitlines() if line]

    def delete(self, uri) -> None:
        """Everything under `uri`; nothing there is not an error."""
        self._run("delete", self._path(uri))

    def copy(self, source, uri) -> None:
        """Every file under the local directory `source` to the same path under `uri`."""
        self._run("copy", source, self._path(uri))

    def sync(self, source, uri) -> None:
        """`copy`, and what is under `uri` and not under `source` goes."""
        self._run("sync", source, self._path(uri))

    def check(self, source, uri) -> bool:
        """Whether every file under `source` is under `uri` with the same content; more there is fine."""
        proc = self._call("check", "--one-way", source, self._path(uri))
        if proc.returncode in (0, 1, *self._NOT_FOUND):  # 1: differences found
            return proc.returncode == 0
        raise RuntimeError(f"rclone check failed (exit {proc.returncode}): {_tail(proc)}")


def _tail(proc):
    return " | ".join((proc.stderr or proc.stdout).strip().splitlines()[-3:])
