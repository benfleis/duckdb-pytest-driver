"""MinIO, an S3-compatible object store, as a Service.

The credentials are those of a local emulator, not secrets, but they reach a test only through a
credential all the same, because an existing instance may be anyone's.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..host import docker
from ..resources import ON_FAILURE, Service, ServiceDef

_IMAGE = os.environ.get("DUCKTEST_MINIO_IMAGE", "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z-cpuv1")


def minio_settings(**overrides):
    """How one instance is reached and started; `MINIO_*` of the environment give the defaults."""
    port = int(overrides.get("port", os.environ.get("DUCKTEST_MINIO_S3_PORT", "9000")))
    # Where a started instance is published, and so where it is reached. With a docker daemon that
    # is not on this machine's loopback (DOCKER_HOST), this is the address of the daemon's host as
    # seen from here; publishing on that one address also keeps the port off the host's other networks.
    host = overrides.get("host", os.environ.get("DUCKTEST_SERVICE_HOST", "127.0.0.1"))
    endpoint = overrides.get("endpoint", f"http://{host}:{port}").rstrip("/")
    return {
        "endpoint": endpoint,
        "region": overrides.get("region", os.environ.get("DUCKTEST_MINIO_REGION", "us-east-1")),
        "access_key": overrides.get("access_key", os.environ.get("DUCKTEST_MINIO_ACCESS_KEY", "admin")),
        "secret_key": overrides.get("secret_key", os.environ.get("DUCKTEST_MINIO_SECRET_KEY", "password")),
        "bucket": overrides.get("bucket", os.environ.get("DUCKTEST_MINIO_BUCKET", "ducktest")),
        "container": overrides["container"],
        "port": port,
        "publish": f"{host}:{port}",
    }


def minio_alive(settings):
    try:
        with urllib.request.urlopen(f"{settings['endpoint']}/minio/health/ready", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _s3(settings):
    import boto3  # the [s3] extra; imported here so a run without this service needs no boto3
    from botocore.config import Config

    return boto3.resource(
        "s3",
        endpoint_url=settings["endpoint"],
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
        config=Config(s3={"addressing_style": "path"}),
    )


def _start(settings):
    docker("rm", "-f", settings["container"], check=False)  # a container leaked by an interrupted run holds the port
    docker(
        "run", "-d", "--name", settings["container"],
        "-p", f"{settings['publish']}:9000",
        "-e", f"MINIO_ROOT_USER={settings['access_key']}",
        "-e", f"MINIO_ROOT_PASSWORD={settings['secret_key']}",
        "--tmpfs", "/data:size=2g",  # MinIO's free-drive check otherwise depends on the host's docker storage
        _IMAGE, "server", "/data",
    )  # fmt: skip


def _stop(settings):
    docker("rm", "-f", settings["container"], check=False)


def _setup(settings):
    try:
        _s3(settings).Bucket(settings["bucket"]).create()
    except Exception as exc:
        # Create and see, not look and then create: two runs on one instance would race.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


@dataclass(eq=False)
class MinioDef(ServiceDef):
    """MinIO in a docker container named for the run, with its bucket made. It outputs `endpoint`,
    `bucket` and `region`; its keys come from the environment (DUCKTEST_MINIO_ACCESS_KEY,
    DUCKTEST_MINIO_SECRET_KEY) and reach a test through `credential(id, scopes=[...])`."""

    kind = "minio"
    detached = True
    attributes = ("endpoint", "bucket", "region")
    secret_attributes = ("access_key", "secret_key")
    delivers = {"AWS_ACCESS_KEY_ID": "access_key", "AWS_SECRET_ACCESS_KEY": "secret_key", "AWS_REGION": "region"}

    id: str = "minio"
    bucket: str = "ducktest"
    port: int | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE


def _container(service_id, run_token):
    return f"ducktest-{service_id}-{run_token}"


class Minio(Service):
    """A stopped container is kept when `retain` keeps it; dealloc removes it."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        overrides = {"bucket": definition.bucket, "container": _container(definition.id, run_token)}
        if definition.port is not None:
            overrides["port"] = definition.port
        if definition.address is not None:
            overrides["endpoint"] = definition.address
        self.settings = minio_settings(**overrides)

    @property
    def handle(self):
        return self.settings["container"]

    @property
    def address(self):
        return self.settings["endpoint"]

    def create(self):
        super().create()
        _setup(self.settings)

    def start(self):
        if self.run_token is None:
            raise ValueError(f"service {self.id}: its container is named for the run, so it needs the run_token")
        _start(self.settings)

    def stop(self):
        docker("stop", self.handle, check=False)

    def dealloc(self):
        _stop(self.settings)

    def read(self):
        if not self.existing and docker("inspect", self.handle, check=False).returncode != 0:
            return None
        return {name: self.settings[name] for name in (*self.definition.attributes, *self.definition.secret_attributes)}

    def ready(self):
        return minio_alive(self.settings)

    def list(self, run_token):
        name = _container(self.id, run_token)
        found = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", check=False)
        return [n for n in found.stdout.split() if n == name]


MinioDef.instance = Minio
