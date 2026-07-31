"""MinIO (S3-compatible object store) as a ducktest service — the S3 sibling of ``azurite``.

AUTH VARIES per consumer, so everything is env-overridable and settable through the attach override
map: ice-fixture and ducklake use ``admin``/``password`` on ``:9000``, ice-nessie uses
``minioadmin``/``minioadmin`` on ``:9002``. The defaults here match the common case. These are LOCAL
dev credentials, not secrets — the emulator authenticates against exactly these, and they are
overridable, so hardcoding the default is config.

duckdb consumes it as ``CREATE SECRET (TYPE S3, KEY_ID …, SECRET …, ENDPOINT '127.0.0.1:9000',
URL_STYLE 'path', USE_SSL 0)`` (or the ``SET s3_*`` equivalents). Unlike Azure Blob, MinIO does not
auto-create a bucket, so the managed boot pre-creates the default one. See ``docs/SERVICES.md``.
"""

import json
import os
import re
import tempfile
import urllib.error
import urllib.request

from ..steps import step
from ..suites import service
from ..tools.rclone import Remote, mkdir, object_prefix, write_conf
from ._docker import docker as _docker
from ._images import ghcr_ref, register_image, served_image
from ._process import wait_until

# LOCAL dev credentials + fixed host ports, all env-overridable. Defaults match ice-fixture/ducklake.
ACCESS_KEY = os.environ.get("DUCKTEST_MINIO_ACCESS_KEY", "admin")
SECRET_KEY = os.environ.get("DUCKTEST_MINIO_SECRET_KEY", "password")
REGION = os.environ.get("DUCKTEST_MINIO_REGION", "us-east-1")
DEFAULT_BUCKET = os.environ.get("DUCKTEST_MINIO_BUCKET", "ducktest")
# Optional wildcard-resolved base domain for virtual-host bucket access. MinIO expects the bare FQDN;
# clients address buckets as ``<bucket>.<domain>``.
DOMAIN = os.environ.get("DUCKTEST_MINIO_DOMAIN") or None

# Driver-owned image: `images/minio/Dockerfile` layers `mc` into the server image.
# A DUCKTEST_MINIO_IMAGE override must also carry `mc`.
IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "minio")
# Names both upstreams the Dockerfile bakes in; bump it whenever either ARG moves, or a rebuild
# overwrites a published tag with different contents.
PIN = os.environ.get("DUCKTEST_MINIO_PIN", "2026-07-28")
GHCR_IMAGE = ghcr_ref("minio", PIN)  # ghcr.io/<ns>/ducktest-minio:<pin> — ns via DUCKTEST_IMAGE_NS
register_image("minio", "build", IMAGE_DIR, PIN)
# `build-images` tags its local build with this same ref, so local and published are interchangeable.
IMAGE = os.environ.get("DUCKTEST_MINIO_IMAGE", served_image("minio", GHCR_IMAGE, PIN))
CONTAINER = os.environ.get("DUCKTEST_MINIO_CONTAINER", "ducktest-minio")
S3_PORT = int(os.environ.get("DUCKTEST_MINIO_S3_PORT", "9000"))
CONSOLE_PORT = int(os.environ.get("DUCKTEST_MINIO_CONSOLE_PORT", "9001"))
# A healthy boot is ~1s; 20s is a generous ceiling that still fails fast rather than hanging.
_READY_TIMEOUT_S = int(os.environ.get("DUCKTEST_MINIO_READY_TIMEOUT_S", "20"))
# `/data` is a RAM-backed tmpfs so MinIO's minimum-free-drive check sees a clean, sized, empty drive:
# a full/small host docker fs otherwise returns `507 XMinioStorageFull`. Allocated lazily — raise the
# cap for large seed datasets.
TMPFS_SIZE = os.environ.get("DUCKTEST_MINIO_TMPFS_SIZE", "2g")


def minio_block(**overrides):
    """Build MinIO's service block from an override map — the single source of block truth.

    ``s3_endpoint`` (host:port, no scheme — what duckdb's ``ENDPOINT`` wants) is DERIVED from
    ``endpoint``, so overriding the endpoint keeps them consistent; that's why this is a function and
    not a dict (docs/SERVICES.md). ``domain`` enables MinIO's virtual-host routing while ``url_style``
    stays path by default; consumers can exercise either style against the same endpoint.
    """
    port = int(overrides.get("port", S3_PORT))
    domain = overrides.get("domain", DOMAIN) or None
    if domain and any(token in domain for token in (":", "/", ",")):
        raise ValueError("minio domain must be one bare FQDN without a scheme, port, path, or comma")
    endpoint = (overrides.get("endpoint") or f"http://{domain or '127.0.0.1'}:{port}").rstrip("/")
    b = {
        "access_key": overrides.get("access_key", ACCESS_KEY),
        "secret_key": overrides.get("secret_key", SECRET_KEY),
        "region": overrides.get("region", REGION),
        "domain": domain,
        "port": port,
        "endpoint": endpoint,  # scheme'd http URL — for rclone + the health probe
        "s3_endpoint": endpoint.split("://", 1)[-1],  # host:port — for duckdb ENDPOINT / SET s3_endpoint
        "url_style": "path",  # conservative default; a configured domain also accepts vhost requests
        "use_ssl": False,  # http, not https
        "bucket": overrides.get("bucket", DEFAULT_BUCKET),
        "data_prefix": overrides.get("data_prefix", "data").strip("/"),
        "temp_prefix": overrides.get("temp_prefix", "temp").strip("/"),
        "container": overrides.get("container", CONTAINER),
        "mc_alias": overrides.get("mc_alias", "ducktest"),
    }
    return b


def minio_env(block):
    """The generic S3-client env derived from a MinIO block — feed as (part of) a suite's ``to_env`` so
    a bare ``.test`` body gets the creds/endpoint without a ``.py`` driver.

    Ships both the AWS-SDK view (glue/boto/pyarrow read these) and an explicit ``S3_*`` view a body can
    substitute into ``SET s3_*`` / ``CREATE SECRET``. A suite layers its own vars on top.
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
        "S3_DATA_URI": f"s3://{object_prefix(block['bucket'], block['data_prefix'], access='ro')}",
        "S3_TEMP_URI_ROOT": f"s3://{object_prefix(block['bucket'], block['temp_prefix'], access='ro')}",
    }


def minio_init_sql(block, *, redact=False):
    """Init SQL: a ready-to-use S3 secret (parallels ``azurite_init_sql`` — see its docstring for why
    env alone isn't enough). ``redact=True`` masks the key material for the dry-run preview.
    """
    key_id = "<redacted>" if redact else block["access_key"]
    secret = "<redacted>" if redact else block["secret_key"]
    return (
        "CREATE OR REPLACE SECRET ducktest_minio (\n"
        "    TYPE S3,\n"
        f"    KEY_ID '{key_id}',\n"
        f"    SECRET '{secret}',\n"
        f"    ENDPOINT '{block['s3_endpoint']}',\n"
        f"    REGION '{block['region']}',\n"
        f"    URL_STYLE '{block['url_style']}',\n"
        f"    USE_SSL {'true' if block['use_ssl'] else 'false'}\n"
        ");\n"
    )


def minio_alive(block):
    """Liveness probe: ``/minio/health/ready`` returns 200 once MinIO can serve.

    Unlike azurite's "any HTTP response = up", this endpoint is authoritative, so require 200 — a 503
    during startup means not ready yet.
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
    """Create the default bucket — MinIO has none at boot, and any write to a missing one is
    ``NoSuchBucket``. Idempotent: rclone ``mkdir`` on an existing bucket is a no-op.

    Goes through a written rclone.conf rather than the inline address, for the reason in
    :func:`minio_rclone`.
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


def minio_mc(block, *args, check=True):
    """Run ``mc`` inside the MinIO container — the single place that shells out to the client, so
    docker-exec details stay local to this module.

    Managed blocks carry ``container``; an attached service must pass a container override whose image
    contains ``mc``.
    """
    container = block.get("container")
    if not container:
        raise RuntimeError(
            "minio_mc requires block['container']; pass it in --existing-service JSON for attached MinIO"
        )
    return _docker("exec", container, "mc", *args, check=check)


def minio_alias_set(block):
    """Configure the block's mc alias inside the container. Idempotent."""
    return minio_mc(
        block,
        "alias",
        "set",
        block["mc_alias"],
        "http://127.0.0.1:9000",
        block["access_key"],
        block["secret_key"],
    )


def minio_user_add(block, user, password):
    """Create or update a MinIO user."""
    minio_alias_set(block)
    return minio_mc(block, "admin", "user", "add", block["mc_alias"], user, password)


def minio_policy_attach(block, policy, *, user):
    """Attach a canned MinIO policy to a user."""
    minio_alias_set(block)
    return minio_mc(block, "admin", "policy", "attach", block["mc_alias"], policy, "--user", user)


def minio_version_enable(block, bucket):
    """Enable bucket versioning."""
    minio_alias_set(block)
    return minio_mc(block, "version", "enable", f"{block['mc_alias']}/{bucket}")


def minio_anonymous_set(block, policy, bucket):
    """Set anonymous bucket access, e.g. ``policy='public'``."""
    minio_alias_set(block)
    return minio_mc(block, "anonymous", "set", policy, f"{block['mc_alias']}/{bucket}")


def minio_presigned_url(block, object_path, *, bucket=None, expire="24h"):
    """Create a presigned download URL for an object through ``mc share download``."""
    minio_alias_set(block)
    bucket = bucket or block["bucket"]
    target = f"{block['mc_alias']}/{bucket}/{object_path.lstrip('/')}"
    result = minio_mc(block, "share", "download", "--json", "--expire", expire, target)

    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("share", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

    match = re.search(r"https?://\S+", result.stdout)
    if match:
        return match.group(0)
    raise RuntimeError(f"mc did not return a presigned URL for {object_path!r}: {result.stdout.strip()}")


def _start_managed(config, block, *, image, console_port, ready_timeout_s, tmpfs_size):
    """Managed boot: run the image on configured host ports, wait for ready, pre-create the bucket.

    ``docker rm -f`` first, so a container leaked by an interrupted run can't wedge the port. On any
    failure after ``docker run``, tear the half-booted container down before re-raising: startup
    raises before the block reaches the store, and ``_stop_services`` only stops services that got
    that far.
    """
    container = block["container"]
    with step(f"starting MinIO ({image})"):
        _docker("rm", "-f", container, check=False)  # force-remove any leftover
        domain_env = ("-e", f"MINIO_DOMAIN={block['domain']}") if block["domain"] else ()
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{block['port']}:9000",
            "-p",
            f"{console_port}:9001",
            "-e",
            f"MINIO_ROOT_USER={block['access_key']}",
            "-e",
            f"MINIO_ROOT_PASSWORD={block['secret_key']}",
            *domain_env,
            "--tmpfs",
            f"/data:size={tmpfs_size}",  # see TMPFS_SIZE
            image,
            "server",
            "/data",
            "--console-address",
            ":9001",
        )
        try:
            wait_until(
                lambda: minio_alive(block),
                ready_timeout_s,
                lambda: (
                    f"MinIO container {container!r} did not become ready on "
                    f"{block.get('endpoint')} after {ready_timeout_s}s (image {image})."
                ),
            )
            _ensure_bucket(block)
        except Exception:
            _docker("rm", "-f", container, check=False)  # don't leak a half-booted container
            raise
    return block


def _stop_managed(config, container):
    """Managed teardown (controller, once): ``rm -f`` = stop + remove."""
    with step("stopping MinIO"):
        _docker("rm", "-f", container, check=False)


def _start(config):
    """Start the default module-configured MinIO instance."""
    return _start_managed(
        config,
        minio_block(),
        image=IMAGE,
        console_port=CONSOLE_PORT,
        ready_timeout_s=_READY_TIMEOUT_S,
        tmpfs_size=TMPFS_SIZE,
    )


def _stop(config):
    """Stop the default module-configured MinIO instance."""
    _stop_managed(config, CONTAINER)


def minio_service(
    key="minio",
    *,
    access_key=ACCESS_KEY,
    secret_key=SECRET_KEY,
    region=REGION,
    bucket=DEFAULT_BUCKET,
    domain=DOMAIN,
    s3_port=S3_PORT,
    console_port=CONSOLE_PORT,
    image=IMAGE,
    container=CONTAINER,
    ready_timeout_s=_READY_TIMEOUT_S,
    tmpfs_size=TMPFS_SIZE,
    data_prefix="data",
    temp_prefix="temp",
    mc_alias="ducktest",
    fixture="minio",
):
    """Build a managed MinIO service with explicit per-instance configuration."""
    defaults = {
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
        "bucket": bucket,
        "domain": domain,
        "port": int(s3_port),
        "container": container,
        "data_prefix": data_prefix,
        "temp_prefix": temp_prefix,
        "mc_alias": mc_alias,
    }
    minio_block(**defaults)  # validate configuration when the service is declared

    def start(config):
        return _start_managed(
            config,
            minio_block(**defaults),
            image=image,
            console_port=int(console_port),
            ready_timeout_s=int(ready_timeout_s),
            tmpfs_size=tmpfs_size,
        )

    def stop(config):
        _stop_managed(config, container)

    def attach(overrides, config):
        return minio_block(**{**defaults, **overrides})

    return service(
        key,
        start=start,
        stop=stop,
        attach=attach,
        alive=minio_alive,
        fixture=fixture,
        to_env=minio_env,
        to_init_sql=minio_init_sql,
    )


MINIO_SERVICE = minio_service()


def minio_rclone(block=None):
    """The rclone :class:`Remote` for a MinIO instance (``s3`` backend, ``Minio`` provider).

    MinIO needs explicit creds + endpoint, and the ``endpoint`` value's ``:``/``/`` mangle rclone's
    inline address, as would a real secret key — so a MinIO remote must be addressed through a config
    file (``write_conf`` + ``config=`` on the verbs), never the inline form. See ``docs/SERVICES.md``
    § object-store seeding.
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
