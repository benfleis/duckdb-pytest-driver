"""MinIO (S3-compatible object store) as a ducktest service — the S3 sibling of ``azurite`` (the
Azure Blob emulator). Same shape: a ``service()`` with managed boot + ``--existing-service`` attach,
one block builder shared by both paths, a liveness probe, and an rclone :class:`Remote` for seeding.

AUTH VARIES per consumer, so everything is parameterized (env-overridable defaults, and the attach
override map). In the existing duckdb setups: ice-fixture and ducklake use ``admin``/``password`` on
``:9000``; ice-nessie uses ``minioadmin``/``minioadmin`` on ``:9002``. The defaults here match the
common case (``admin``/``password``/``:9000``); a suite that needs other creds/port passes them through
``use_service(MINIO_SERVICE, ...)`` / ``--existing-service minio={json}`` / the ``DUCKTEST_MINIO_*`` env.
These are LOCAL dev credentials, not secrets — the local emulator authenticates against exactly these,
and they are overridable, so hardcoding the default is config, not a leak.

duckdb consumes it as ``CREATE SECRET (TYPE S3, KEY_ID …, SECRET …, ENDPOINT '127.0.0.1:9000',
URL_STYLE 'path', USE_SSL 0)`` (or the ``SET s3_*`` equivalents). Unlike Azure Blob, MinIO does NOT
auto-create a bucket, so the managed boot pre-creates the default bucket via rclone (ducktest's
object-store tool — no separate ``mc`` container needed). See ``docs/SERVICES.md``.
"""

import os
import tempfile
import urllib.error
import urllib.request

from ..steps import step
from ..tools.rclone import Remote, mkdir, write_conf
from ..suites import service
from ._docker import docker as _docker, wait_until
from ._images import ghcr_ref, register_image, served_image

# LOCAL dev credentials + fixed host ports, all env-overridable. Defaults match ice-fixture/ducklake.
# (See the module docstring on why hardcoding the default creds is config, not a secret.)
ACCESS_KEY = os.environ.get("DUCKTEST_MINIO_ACCESS_KEY", "admin")
SECRET_KEY = os.environ.get("DUCKTEST_MINIO_SECRET_KEY", "password")
REGION = os.environ.get("DUCKTEST_MINIO_REGION", "us-east-1")
DEFAULT_BUCKET = os.environ.get("DUCKTEST_MINIO_BUCKET", "ducktest")

# Supply chain (docs/PLAN.md, ducktest.resources._images): we MIRROR upstream MinIO into an org-controlled
# ghcr tag and serve from there. UPSTREAM is the plain, MULTI-ARCH RELEASE (linux/amd64 + arm64) — same
# version duckdb's httpfs S3 tests pin (d/httpfs/scripts/minio_s3.yml), minus their amd64-only `-cpuv1`
# suffix, so the mirror serves both arches. The ghcr PIN reuses the upstream RELEASE for clean provenance.
# (An AVX-less amd64 host that SIGILLs on the default build can override DUCKTEST_MINIO_UPSTREAM to the
# `-cpuv1` variant — but that one is amd64-only.)
UPSTREAM_IMAGE = os.environ.get("DUCKTEST_MINIO_UPSTREAM", "minio/minio:RELEASE.2025-09-07T16-13-09Z")
PIN = os.environ.get("DUCKTEST_MINIO_PIN", "RELEASE.2025-09-07T16-13-09Z")  # ghcr tag = upstream RELEASE
GHCR_IMAGE = ghcr_ref("minio", PIN)  # ghcr.io/<ns>/ducktest-minio:<pin> — ns via DUCKTEST_IMAGE_NS
register_image("minio", "mirror", UPSTREAM_IMAGE, PIN)
# What MinIO RUNS: follows DUCKTEST_IMAGE_SOURCE (upstream until the mirror is published, then ghcr — the
# PLAN [c] flip). Per-instance override via DUCKTEST_MINIO_IMAGE.
IMAGE = os.environ.get("DUCKTEST_MINIO_IMAGE", served_image("minio", UPSTREAM_IMAGE, PIN))
CONTAINER = os.environ.get("DUCKTEST_MINIO_CONTAINER", "ducktest-minio")
S3_PORT = int(os.environ.get("DUCKTEST_MINIO_S3_PORT", "9000"))
CONSOLE_PORT = int(os.environ.get("DUCKTEST_MINIO_CONSOLE_PORT", "9001"))
# A healthy MinIO boot is ~1s; 20s is a generous ceiling that still FAILS FAST rather than hanging.
_READY_TIMEOUT_S = int(os.environ.get("DUCKTEST_MINIO_READY_TIMEOUT_S", "20"))
# `/data` is a RAM-backed tmpfs (see _start): decouples MinIO's minimum-free-drive check from the host's
# docker storage (a full/small docker fs otherwise 507s `XMinioStorageFull`), and it's ephemeral, which
# is right for a test emulator. The cap is a ceiling, allocated lazily — raise it for large seed datasets.
TMPFS_SIZE = os.environ.get("DUCKTEST_MINIO_TMPFS_SIZE", "2g")


def minio_block(**overrides):
    """Build MinIO's service block from an override map — the single source of block truth.

    Empty overrides => the all-defaults block. ``endpoint=URL`` points at a moved/attached instance;
    ``access_key=``/``secret_key=``/``port=``/``region=``/``bucket=`` override the rest. ``s3_endpoint``
    (host:port, no scheme — what duckdb's ``ENDPOINT`` wants) is DERIVED from ``endpoint``, so overriding
    the endpoint keeps them consistent (that's why this is a function, not a dict — docs/SERVICES.md).
    MinIO on ``127.0.0.1`` needs path-style + no TLS, so ``url_style``/``use_ssl`` are fixed accordingly.
    """
    port = int(overrides.get("port", S3_PORT))
    endpoint = (overrides.get("endpoint") or f"http://127.0.0.1:{port}").rstrip("/")
    b = {
        "access_key": overrides.get("access_key", ACCESS_KEY),
        "secret_key": overrides.get("secret_key", SECRET_KEY),
        "region": overrides.get("region", REGION),
        "port": port,
        "endpoint": endpoint,  # scheme'd http URL — for rclone + the health probe
        "s3_endpoint": endpoint.split("://", 1)[-1],  # host:port — for duckdb ENDPOINT / SET s3_endpoint
        "url_style": "path",  # local MinIO can't do virtual-host addressing
        "use_ssl": False,  # http, not https
        "bucket": overrides.get("bucket", DEFAULT_BUCKET),
    }
    return b


def minio_env(block):
    """The generic S3-client env derived from a MinIO block — feed as (part of) a suite's ``to_env``
    (``use_service(MINIO_SERVICE, to_env=…)``) so a bare ``.test`` body gets the creds/endpoint without a
    ``.py`` driver. Ships both the AWS-SDK view (glue/boto/pyarrow read these) and an explicit ``S3_*``
    view a body can substitute into ``SET s3_*`` / ``CREATE SECRET``. A suite layers its own vars on top;
    which keys a given suite actually needs is adjustable via the ``to_env`` it passes. See SERVICES.md.
    """
    return {
        "AWS_ACCESS_KEY_ID": block["access_key"],
        "AWS_SECRET_ACCESS_KEY": block["secret_key"],
        "AWS_REGION": block["region"],
        "AWS_ENDPOINT_URL": block["endpoint"],
        "S3_ENDPOINT": block["s3_endpoint"],
        "S3_ACCESS_KEY_ID": block["access_key"],
        "S3_SECRET_ACCESS_KEY": block["secret_key"],
        "S3_REGION": block["region"],
        "S3_URL_STYLE": block["url_style"],
        "S3_USE_SSL": "1" if block["use_ssl"] else "0",
    }


def minio_alive(block):
    """Liveness probe: MinIO's ``/minio/health/ready`` returns 200 once it can serve.

    A 200 means up; a non-200 (503 during startup) or a connection error means not ready yet. Unlike
    azurite's "any HTTP response = up", MinIO's health endpoint is authoritative, so we require 200.
    """
    ep = (block.get("endpoint") or f"http://127.0.0.1:{S3_PORT}").rstrip("/")
    try:
        with urllib.request.urlopen(f"{ep}/minio/health/ready", timeout=3) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False  # server responded but not ready (e.g. 503)
    except (urllib.error.URLError, OSError):
        return False


def _ensure_bucket(block):
    """Create the default bucket — MinIO has none at boot (unlike Azure Blob's auto-vivify), and any
    write to a missing bucket is a NoSuchBucket error. Uses rclone (ducktest's object-store tool), so no
    separate ``mc`` container is needed. Idempotent: rclone ``mkdir`` on an existing bucket is a no-op.

    Goes through a written rclone.conf (not the inline address): MinIO's ``endpoint`` URL breaks rclone's
    inline connection-string form, so the remote must be addressed by a config-file stanza + ``config=``.
    """
    remote = minio_rclone(block)
    fd, conf = tempfile.mkstemp(prefix="ducktest-minio-", suffix=".conf")
    os.close(fd)
    try:
        write_conf(remote, conf)
        with step(f"creating MinIO bucket {block['bucket']!r}"):
            mkdir(remote, block["bucket"], config=conf)
    finally:
        os.unlink(conf)


def _start(config):
    """Managed boot: run the standard MinIO image on the fixed host ports; return its block.

    ALWAYS_CREATE (``docker rm -f`` first => fresh) so a container leaked by an interrupted run can't
    wedge the port. `/data` is a tmpfs (RAM-backed) so MinIO's minimum-free-drive check sees a clean,
    sized, empty drive regardless of the host's docker storage — a full/small docker fs otherwise returns
    ``507 XMinioStorageFull``; tmpfs is ephemeral, which suits a test emulator (we ``rm -f`` on teardown).
    Boots with the module's root creds, waits until ready, then pre-creates the default bucket. If either
    readiness or the bucket-create fails after ``docker run``, tear the half-booted container back down
    before re-raising — ``_start`` raises before the block is stored, so ``_stop_services`` would never
    reach it (it only stops services whose block is in the store; ``_ensure_bucket`` is a real second
    failure point azurite lacks). Returns ``minio_block(...)`` — identical shape to the attach path.
    """
    with step(f"starting MinIO ({IMAGE})"):
        _docker("rm", "-f", CONTAINER, check=False)  # force-remove any leftover
        _docker(
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            f"{S3_PORT}:9000",
            "-p",
            f"{CONSOLE_PORT}:9001",
            "-e",
            f"MINIO_ROOT_USER={ACCESS_KEY}",
            "-e",
            f"MINIO_ROOT_PASSWORD={SECRET_KEY}",
            "--tmpfs",
            f"/data:size={TMPFS_SIZE}",  # RAM-backed /data — sidesteps host-disk `507 XMinioStorageFull`
            IMAGE,
            "server",
            "/data",
            "--console-address",
            ":9001",
        )
        try:
            block = minio_block()
            wait_until(
                lambda: minio_alive(block),
                _READY_TIMEOUT_S,
                lambda: (
                    f"MinIO container {CONTAINER!r} did not become ready on "
                    f"{block.get('endpoint')} after {_READY_TIMEOUT_S}s (image {IMAGE})."
                ),
            )
            _ensure_bucket(block)
        except Exception:
            _docker("rm", "-f", CONTAINER, check=False)  # don't leak a half-booted container
            raise
    return block


def _stop(config):
    """Managed teardown (controller, once): stop AND remove the container (rm -f = stop + remove)."""
    with step("stopping MinIO"):
        _docker("rm", "-f", CONTAINER, check=False)


MINIO_SERVICE = service(
    "minio",
    start=_start,
    stop=_stop,
    attach=lambda overrides, config: minio_block(**overrides),
    alive=minio_alive,
    fixture="minio",
)


def minio_rclone(block=None):
    """The rclone :class:`Remote` for a MinIO instance (rclone ``s3`` backend, ``Minio`` provider).

    Unlike azurite's ``use_emulator`` inline shortcut, MinIO always needs explicit creds + endpoint, and
    the ``endpoint`` value itself (``http://…`` — the ``:`` / ``/`` chars) mangles rclone's inline
    address, as would a real secret key. So a MinIO remote must be addressed through a config file
    (``write_conf`` + ``config=`` on the verbs), not the inline form (this is what ``_ensure_bucket``
    does, and what real seeding must do). See ``docs/SERVICES.md`` § object-store seeding.
    """
    b = block or minio_block()
    return Remote(
        "minio",
        {
            "type": "s3",
            "provider": "Minio",
            "access_key_id": b["access_key"],
            "secret_access_key": b["secret_key"],
            "endpoint": b["endpoint"],
            "region": b["region"],
        },
    )
