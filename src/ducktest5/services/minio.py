"""MinIO, an S3-compatible object store, as a `Service`. The block has the field names of the pytest
driver's `ducktest.resources.minio`, so one `--existing-service minio={json}` serves both.

The credentials are those of a local emulator, not secrets, but they are redacted in reports all the
same, because an existing instance may be anyone's.
"""

import os
import urllib.error
import urllib.request

from ..declare import Service
from ..host import docker

_IMAGE = os.environ.get("DUCKTEST_MINIO_IMAGE", "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z-cpuv1")


def minio_block(**overrides):
    port = int(overrides.get("port", os.environ.get("DUCKTEST_MINIO_S3_PORT", "9000")))
    # Where a started instance is published, and so where it is reached. With a docker daemon that
    # is not on this machine's loopback (DOCKER_HOST), this is the address of the daemon's host as
    # seen from here; publishing on that one address also keeps the port off the host's other networks.
    host = overrides.get("host", os.environ.get("DUCKTEST_SERVICE_HOST", "127.0.0.1"))
    endpoint = overrides.get("endpoint", f"http://{host}:{port}").rstrip("/")
    return {
        "endpoint": endpoint,
        "s3_endpoint": endpoint.split("://", 1)[-1],  # host:port, what DuckDB's ENDPOINT takes
        "use_ssl": endpoint.startswith("https://"),
        "url_style": overrides.get("url_style", "path"),
        "region": overrides.get("region", os.environ.get("DUCKTEST_MINIO_REGION", "us-east-1")),
        "access_key": overrides.get("access_key", os.environ.get("DUCKTEST_MINIO_ACCESS_KEY", "admin")),
        "secret_key": overrides.get("secret_key", os.environ.get("DUCKTEST_MINIO_SECRET_KEY", "password")),
        "bucket": overrides.get("bucket", os.environ.get("DUCKTEST_MINIO_BUCKET", "ducktest")),
        "temp_prefix": overrides.get("temp_prefix", "temp"),
        "container": overrides.get("container", os.environ.get("DUCKTEST_MINIO_CONTAINER", "ducktest-minio")),
        "port": port,
        "publish": f"{host}:{port}",
    }


def minio_alive(block):
    try:
        with urllib.request.urlopen(f"{block['endpoint']}/minio/health/ready", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _s3(block):
    import boto3  # the [s3] extra; imported here so a run without this service needs no boto3
    from botocore.config import Config

    return boto3.resource(
        "s3",
        endpoint_url=block["endpoint"],
        aws_access_key_id=block["access_key"],
        aws_secret_access_key=block["secret_key"],
        region_name=block["region"],
        config=Config(s3={"addressing_style": block["url_style"]}),
    )


def _start(block):
    docker("rm", "-f", block["container"], check=False)  # a container leaked by an interrupted run holds the port
    docker(
        "run", "-d", "--name", block["container"],
        "-p", f"{block['publish']}:9000",
        "-e", f"MINIO_ROOT_USER={block['access_key']}",
        "-e", f"MINIO_ROOT_PASSWORD={block['secret_key']}",
        "--tmpfs", "/data:size=2g",  # MinIO's free-drive check otherwise depends on the host's docker storage
        _IMAGE, "server", "/data",
    )  # fmt: skip


def _stop(block):
    docker("rm", "-f", block["container"], check=False)


def _setup(block):
    try:
        _s3(block).Bucket(block["bucket"]).create()
    except Exception as exc:
        # Create and see, not look and then create: two runs on one instance would race.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


def _clean(block, uri):
    """Remove the object at `uri` and everything below it. Only ever under the bucket's temp prefix."""
    root = _location(block) + "/"
    below = uri[len(root) :].strip("/") if uri.startswith(root) else ""
    if not below:
        raise ValueError(f"{uri!r} is not below {root}: only what is there is removed")
    key = f"{block['temp_prefix']}/{below}"
    bucket = _s3(block).Bucket(block["bucket"])
    bucket.Object(key).delete()
    bucket.objects.filter(Prefix=key + "/").delete()  # with the `/`: `.../t` must not take `.../t2` with it


def _location(block, *parts):
    return "/".join([f"s3://{block['bucket']}", block["temp_prefix"], *parts])


def duckdb_secret(block, name="ducktest_minio"):
    """The DuckDB secret for this instance. A remote ATTACH also needs READ_WRITE to write."""
    return (
        f"CREATE OR REPLACE SECRET {name} (TYPE s3, KEY_ID '{block['access_key']}', SECRET '{block['secret_key']}', "
        f"REGION '{block['region']}', ENDPOINT '{block['s3_endpoint']}', URL_STYLE '{block['url_style']}', "
        f"USE_SSL {'true' if block['use_ssl'] else 'false'});"
    )


def minio_service(id="minio"):
    return Service(
        id,
        block=minio_block,
        alive=minio_alive,
        start=_start,
        stop=_stop,
        setup=_setup,
        clean=_clean,
        location=_location,
        secrets=("access_key", "secret_key"),
        outlives_the_process=True,  # a container: it stays up when the run that started it ends
    )
