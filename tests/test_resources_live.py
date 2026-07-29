"""Live validation of the shipped ``ducktest.resources`` services (azurite, minio) — the opt-in
docker tier.

Unlike the rest of the driver's self-tests (offline, stub ``unittest`` binary, no docker), these boot
REAL containers, so they are gated behind the ``--run-docker`` flag (see ``tests/conftest.py``), which
also skips them unless ``docker`` + ``rclone`` are both on PATH. The default ``pytest tests/`` run skips
them (they no-op in the sandbox / any box without docker); a docker-capable CI job runs them with
``--run-docker``. The ``docker`` marker here is just what that gate keys on (and lets you ``-m docker``).

Each resource is driven through its full lifecycle via its own ``service()`` descriptor — the same API a
consumer uses — so this validates the shipped resource code AND the service layer (start / alive / attach
/ stop) against a real backend, not a fake. A real object round-trip (rclone seed a file, read it back)
proves the block's creds + endpoint actually authenticate, which no offline test can.

Add a new resource by appending one ``pytest.param`` to ``_RESOURCES``.
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import pytest

from ducktest.resources import azurite, minio
from ducktest.tools import rclone

# Just the marker — the opt-in gate (`--run-docker`) + the docker/rclone-present check live in
# tests/conftest.py's pytest_collection_modifyitems, so there's one place that decides whether to run.
pytestmark = pytest.mark.docker


@dataclass
class _Res:
    """One shippable resource to validate: its ``service()`` descriptor plus the two things the
    descriptor doesn't carry — how to build its rclone remote from a block, which block key names the
    container/bucket, and the managed container name (to assert it's gone after teardown)."""

    service: object  # the service() descriptor: start / stop / attach / alive / key
    rclone_remote: object  # (block) -> rclone.Remote
    bucket_key: str  # block key holding the container/bucket name
    container: str  # managed container name (module constant)


_RESOURCES = [
    pytest.param(
        _Res(azurite.AZURITE_SERVICE, azurite.rclone_remote, "container", azurite.CONTAINER),
        id="azurite",
    ),
    pytest.param(
        _Res(minio.MINIO_SERVICE, minio.minio_rclone, "bucket", minio.CONTAINER),
        id="minio",
    ),
]


def _container_running(name):
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return name in out.stdout.split()


def _roundtrip(res, block):
    """Prove the live block actually works: seed a small file into the container/bucket and read it back
    through rclone (which exercises the block's creds + endpoint against the real server)."""
    remote = res.rclone_remote(block)
    container = block[res.bucket_key]
    src = tempfile.mkdtemp(prefix="ducktest-live-src-")
    fd, conf = tempfile.mkstemp(prefix="ducktest-live-", suffix=".conf")
    os.close(fd)
    try:
        with open(os.path.join(src, "probe.txt"), "w") as f:
            f.write("ducktest-live-probe")
        rclone.write_conf(remote, conf)
        prefix = rclone.seed(remote, src, container, "probe", access="ro", config=conf)  # DCL + DML
        listing = rclone.ls(remote, prefix, config=conf)
        assert "probe.txt" in listing, f"seeded object not found under {prefix!r}: {listing!r}"
    finally:
        try:
            rclone.purge(remote, container, config=conf)  # best-effort cleanup of the probe data
        except Exception:
            pass
        os.unlink(conf)
        shutil.rmtree(src, ignore_errors=True)


@pytest.mark.parametrize("res", _RESOURCES)
def test_resource_full_lifecycle(res):
    """Bring up → ready → attach → real round-trip → bring down → gone."""
    svc = res.service

    try:
        # start INSIDE the try: a partial boot (docker run succeeds, then _wait_alive/_ensure_bucket
        # raises) must still hit the finally so the container is torn down, not leaked.
        block = svc.start(None)  # managed boot (docker run; minio also creates the bucket)
        assert svc.alive(block), f"{svc.key}: not alive after start ({block.get('endpoint')})"

        # attach: rebuild the block pointing at the same running instance — the --existing-service path.
        # It must produce a live, equivalent block without booting or tearing anything down.
        attached = svc.attach({"endpoint": block["endpoint"]}, None)
        assert svc.alive(attached), f"{svc.key}: attach block not alive"

        _roundtrip(res, block)
    finally:
        svc.stop(None)  # managed teardown (docker rm -f)

    assert not _container_running(res.container), f"{svc.key}: container {res.container!r} still present after stop"


def test_minio_mc_admin_helpers():
    """Exercise the MinIO mc helper layer against the real managed container."""
    block = None
    try:
        block = minio.MINIO_SERVICE.start(None)

        minio.minio_alias_set(block)
        minio.minio_user_add(block, "ducktest_user", "ducktest_user_password")
        minio.minio_policy_attach(block, "readwrite", user="ducktest_user")
        minio.minio_version_enable(block, block["bucket"])
        minio.minio_anonymous_set(block, "public", block["bucket"])

        user_info = minio.minio_mc(block, "admin", "user", "info", block["mc_alias"], "ducktest_user").stdout
        anonymous_info = minio.minio_mc(block, "anonymous", "get", f"{block['mc_alias']}/{block['bucket']}").stdout
        version_info = minio.minio_mc(block, "version", "info", f"{block['mc_alias']}/{block['bucket']}").stdout

        assert "Status: enabled" in user_info
        assert "PolicyName: readwrite" in user_info
        assert "is `public`" in anonymous_info
        assert "versioning is enabled" in version_info
    finally:
        minio.MINIO_SERVICE.stop(None)

    assert not _container_running(minio.CONTAINER), f"minio: container {minio.CONTAINER!r} still present after stop"
