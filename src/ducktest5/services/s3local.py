"""A local S3-compatible server, as a Service: run in docker, or found running at an address.

It runs as RustFS (`DUCKTEST_S3_LOCAL_IMAGE` overrides the image). A server that already answers where
the run reaches it is used as it is, so a machine with no docker runs against one started elsewhere.
A test does not see the server directly; it sees the endpoint, the bucket, and the credential the
service produces.

The keys are those of a local server, not secrets, but they reach a test only through a
credential all the same, because an existing instance may be anyone's.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..host import docker, has_docker, in_url, service_bind, service_host
from ..resources import ON_FAILURE, Service, ServiceDef, ServiceNotUp, StorageDef
from ..resources.remote import S3Access, remote_for

_log = logging.getLogger(__name__)


RUSTFS_IMAGE = "rustfs/rustfs:1.0.0"
RUSTFS_KEYS = ("RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY")  # the variables the server reads access/secret key from
RUSTFS_ENV = (("RUSTFS_VOLUMES", "/data"), ("RUSTFS_ADDRESS", ":9000"), ("RUSTFS_CONSOLE_ENABLE", "false"))


def s3_settings(**overrides):
    """How one instance is reached and started; `DUCKTEST_S3_LOCAL_*` of the environment give the defaults."""
    port = int(overrides.get("port", os.environ.get("DUCKTEST_S3_LOCAL_PORT", "9000")))
    # Published on one address, not all, to keep the port off the host's other networks.
    bind = overrides.get("bind", service_bind())
    host = overrides.get("host", service_host())
    endpoint = overrides.get("endpoint", f"http://{in_url(host)}:{port}").rstrip("/")
    return {
        "endpoint": endpoint,
        "region": overrides.get("region", os.environ.get("DUCKTEST_S3_LOCAL_REGION", "us-east-1")),
        "access_key": overrides.get("access_key", os.environ.get("DUCKTEST_S3_LOCAL_ACCESS_KEY", "admin")),
        "secret_key": overrides.get("secret_key", os.environ.get("DUCKTEST_S3_LOCAL_SECRET_KEY", "password")),
        "bucket": overrides.get("bucket", os.environ.get("DUCKTEST_S3_LOCAL_BUCKET", "ducktest")),
        "container": overrides["container"],
        "port": port,
        "publish": f"{in_url(bind)}:{port}",
    }


def s3_answers(endpoint):
    """Whether an S3 server answers at `endpoint`. The server refuses an unsigned request, and
    that refusal is the answer; a 5xx is one that is not ready yet."""
    try:
        with urllib.request.urlopen(f"{endpoint}/", timeout=3):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


def _start(settings):
    # A container leaked by an interrupted run holds the port; `rm -f` succeeding here means one was found.
    if docker("rm", "-f", settings["container"], check=False).returncode == 0:
        _log.warning("s3: removing a container left by an earlier run: %s", settings["container"])
    given = zip(RUSTFS_KEYS, (settings["access_key"], settings["secret_key"]))
    env = [a for variable, value in (*given, *RUSTFS_ENV) for a in ("-e", f"{variable}={value}")]
    docker(
        "run", "-d", "--name", settings["container"],
        "-p", f"{settings['publish']}:9000",
        *env,
        os.environ.get("DUCKTEST_S3_LOCAL_IMAGE", RUSTFS_IMAGE),
    )  # fmt: skip


@dataclass(eq=False)
class S3LocalDef(ServiceDef):
    """A local S3-compatible server with its bucket made: one that already answers where the run
    reaches it (DUCKTEST_SERVICE_HOST, DUCKTEST_S3_LOCAL_PORT), else in a docker container named for the
    run, or the one `.as_existing(address=...)` names. It runs as RustFS. Its attributes are
    `endpoint`, `bucket` and `region`; its keys come from the environment (DUCKTEST_S3_LOCAL_ACCESS_KEY,
    DUCKTEST_S3_LOCAL_SECRET_KEY) and reach a test through `credential(id, scopes=[...])`.
    `storage(id, prefix)` declares a storage in its bucket."""

    kind = "s3_local"
    detached = True
    attributes = ("endpoint", "bucket", "region")
    secret_attributes = ("access_key", "secret_key")
    delivers = {"AWS_ACCESS_KEY_ID": "access_key", "AWS_SECRET_ACCESS_KEY": "secret_key", "AWS_REGION": "region"}

    id: str = "s3"
    bucket: str = "ducktest"
    port: int | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE

    def storage(self, id: str, prefix: str = "", **fields) -> StorageDef:
        """A storage at `s3://<bucket>/<prefix>` on this server."""
        root = f"s3://{{{self.id}.bucket}}/{prefix}".rstrip("/")
        return StorageDef(id, root, endpoint=f"{{{self.id}.endpoint}}", inputs=[self], **fields)


def s3_local(id: str = "s3", **fields) -> S3LocalDef:
    """A local S3-compatible server; see S3LocalDef."""
    return S3LocalDef(id, **fields)


def _container(service_id, run_token):
    return f"ducktest-{service_id}-{run_token}"


class S3Local(Service):
    """A stopped container is kept when `retain` keeps it; dealloc removes it. One found running is
    never touched: no docker call is made for it."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        overrides = {"bucket": definition.bucket, "container": _container(definition.id, run_token)}
        if definition.port is not None:
            overrides["port"] = definition.port
        if definition.address is not None:
            overrides["endpoint"] = definition.address
        self.settings = s3_settings(**overrides)

    @property
    def handle(self):
        return self.settings["container"]

    @property
    def address(self):
        return self.settings["endpoint"]

    def remote(self):
        """The operations on this server's store, with its own keys."""
        s = self.settings
        return remote_for(S3Access(s["endpoint"], s["region"], lambda: (s["access_key"], s["secret_key"])))

    def create(self):
        super().create()
        self.remote().make_bucket(self.settings["bucket"])

    def start(self):
        if self.run_token is None:
            raise ValueError(f"service {self.id}: its container is named for the run, so it needs the run_token")
        if not has_docker():
            raise ServiceNotUp(
                f"nothing answers at {self.address}, and there is no docker here to start one; start it where "
                f"docker runs (`ducktest5 provision`) and set DUCKTEST_SERVICE_HOST to where it is reached from here"
            )
        _start(self.settings)

    def stop(self):
        docker("stop", self.handle, check=False)

    def dealloc(self):
        docker("rm", "-f", self.handle, check=False)

    def read(self):
        if not self.existing and docker("inspect", self.handle, check=False).returncode != 0:
            return None
        return {name: self.settings[name] for name in (*self.definition.attributes, *self.definition.secret_attributes)}

    def ready(self):
        return s3_answers(self.settings["endpoint"])

    answers = ready

    def list(self, run_token):
        if self.existing:
            return []
        name = _container(self.id, run_token)
        found = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", check=False)
        return [n for n in found.stdout.split() if n == name]


S3LocalDef.instance = S3Local
