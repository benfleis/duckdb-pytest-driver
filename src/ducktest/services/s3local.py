"""A local S3-compatible server, as a Service: run in docker, or found running at an address.

`flavor` names what backs it; RustFS is the only one (`DUCKTEST_S3_LOCAL_IMAGE` overrides its image). A server that already answers where
the run reaches it is used as it is, so a machine with no docker runs against one started elsewhere.
A test does not see the server directly; it sees the endpoint, the bucket, and the credential the
service produces.

The keys are those of a local server, not secrets, but they reach a test only through a
credential all the same, because an existing instance may be anyone's.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .. import names
from ..binary import UsageError
from ..host import docker, has_docker, in_url, service_bind, service_host
from ..resources import NEVER, ON_FAILURE, S3CredentialDef, Service, ServiceDef, ServiceNotUp, StorageDef
from ..resources.remote import S3Access, remote_for
from .parts import Part, Parts, parts_needed
from .s3api import S3Api

_log = logging.getLogger(__name__)


RUSTFS_IMAGE = "rustfs/rustfs:1.0.0"
RUSTFS_KEYS = ("RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY")  # the variables the server reads access/secret key from
RUSTFS_ENV = (("RUSTFS_VOLUMES", "/data"), ("RUSTFS_ADDRESS", ":9000"), ("RUSTFS_CONSOLE_ENABLE", "false"))
FLAVORS = ("rustfs",)


def s3_settings(**overrides):
    """How one instance is reached and started; `DUCKTEST_S3_LOCAL_*` of the environment give the defaults."""
    port = int(overrides.get("port", os.environ.get("DUCKTEST_S3_LOCAL_PORT", "9000")))
    # Published on one address, not all, to keep the port off the host's other networks.
    bind = overrides.get("bind", service_bind())
    host = overrides.get("host", service_host())
    endpoint = overrides.get("endpoint", f"http://{in_url(host)}:{port}").rstrip("/")
    return {
        "endpoint": endpoint,
        "host_port": endpoint.split("://", 1)[-1],  # what an S3 client without a scheme takes as ENDPOINT
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


@dataclass(frozen=True)
class Bucket(Part):
    """A bucket the server holds while the run uses it (see Part): `public` opens it to anonymous
    list and read."""

    noun = "bucket"

    public: bool = False


class _BucketCalls:
    noun, scheme = "bucket", "s3"

    def __init__(self, api, remote):
        self.api, self.copy, self.check = api, remote.copy, remote.check
        self.exists, self.make = api.bucket_exists, api.create_bucket

    def remove(self, name):
        self.api.delete_bucket(name, force=True)

    def is_public(self, part):
        return self.api.is_public(part.name)

    def make_public(self, part):
        self.api.make_public(part.name)


def bucket(name: str, seed=(), public: bool = False, retain: str = NEVER) -> Bucket:
    """A bucket on a local S3 server: `s3_local(buckets=[bucket("data", seed=[("dir", "prefix")])])`."""
    return Bucket(name, tuple(tuple(s) for s in seed), public, retain)


@dataclass(eq=False)
class S3LocalDef(ServiceDef):
    """A local S3-compatible server with its bucket made: one that already answers where the run
    reaches it (DUCKTEST_SERVICE_HOST, DUCKTEST_S3_LOCAL_PORT), else in a docker container named for the
    run, or the one `.as_existing(address=...)` names. `flavor` is what backs it: `rustfs`. Its attributes are
    `endpoint`, `host_port` (the endpoint without its scheme), `bucket` and `region`; its keys come
    from the environment (DUCKTEST_S3_LOCAL_ACCESS_KEY, DUCKTEST_S3_LOCAL_SECRET_KEY) and reach a test
    through `credential(name, scopes=[...])`. `storage(name, prefix)` declares a storage in its bucket.

    `buckets` are more buckets, each a `bucket(...)`, made when the server is and filled from local
    directories. The run removes a bucket it made at the end, but on a server found running, where
    other runs may use it: there it stays. One that is already there
    and holds what it seeds (and is public when asked) is used as it is and never changed; one with
    other content is refused, and blocks only the tests that need it (`parts_needed`). `bucket`
    itself is made when it is not there, and never removed."""

    kind = "s3_local"
    detached = True
    attributes = ("endpoint", "host_port", "bucket", "region")
    secret_attributes = ("access_key", "secret_key")
    credential_type = S3CredentialDef
    delivers = {"key_id": "access_key", "secret": "secret_key", "region": "region"}

    name: str = "s3"
    flavor: str = "rustfs"
    bucket: str = "ducktest"
    buckets: list = field(default_factory=list)
    port: int | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE

    def __post_init__(self):
        super().__post_init__()
        if self.flavor not in FLAVORS:
            raise UsageError(f"service {self.name!r}: flavor {self.flavor!r}; it is one of {', '.join(FLAVORS)}")

    def reached_at(self, address: str) -> S3LocalDef:
        # Cleanup after the run reaches the server for its storages; the buckets the run made are gone.
        return dataclasses.replace(self.as_existing(address), buckets=[])

    def parts(self) -> tuple:
        return tuple(self.buckets)

    def parts_needed(self, inputs) -> list:
        return parts_needed(self, inputs, [b.name for b in self.buckets], "s3")

    def storage(self, name: str, prefix: str = "", bucket: str | None = None, **fields) -> StorageDef:
        """A storage at `s3://<bucket>/<prefix>` on this server; `bucket` names one of `buckets`, else
        it is the server's own."""
        if bucket is not None:
            if not isinstance(bucket, str) and not any(bucket is b for b in self.buckets):
                raise UsageError(
                    f"service {self.name!r}: bucket {getattr(bucket, 'name', bucket)!r} is not one of its buckets; "
                    f"add it to s3_local(buckets=[...])"
                )
            bucket = names.ref(bucket, "bucket", f"service {self.name!r}: storage({name!r})")
            if bucket not in [b.name for b in self.buckets]:
                raise UsageError(f"service {self.name!r}: {bucket!r} is not one of its buckets")
        root = f"s3://{bucket or '{' + self.name + '.bucket}'}/{prefix}".rstrip("/")
        return StorageDef(name, root, endpoint=f"{{{self.name}.endpoint}}", inputs=[self], **fields)


def s3_local(name: str = "s3", **fields) -> S3LocalDef:
    """A local S3-compatible server; see S3LocalDef."""
    return S3LocalDef(name, **fields)


def _container(service_id, run_token):
    return f"ducktest-{service_id}-{run_token}"


class S3Local(Service):
    """A stopped container is kept when `retain` keeps it; dealloc removes it. One found running is
    never touched: no docker call is made for it."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        overrides = {"bucket": definition.bucket, "container": _container(definition.name, run_token)}
        if definition.port is not None:
            overrides["port"] = definition.port
        if definition.address is not None:
            overrides["endpoint"] = definition.address
        self.settings = s3_settings(**overrides)
        self.parts = Parts(definition.name, definition.buckets)
        self.refused = self.parts.refused

    @property
    def made_buckets(self):
        return self.parts.made

    @property
    def handle(self):
        return self.settings["container"]

    @property
    def address(self):
        return self.settings["endpoint"]

    def _keys(self):
        return self.settings["access_key"], self.settings["secret_key"]

    def remote(self):
        """The file operations on this server's store (rclone), with its own keys."""
        return remote_for(S3Access(self.settings["endpoint"], self.settings["region"], self._keys))

    def api(self):
        """The server's own bucket calls: signed S3 API requests."""
        return S3Api(self.settings["endpoint"], self.settings["region"], self._keys)

    def _calls(self):
        return _BucketCalls(self.api(), self.remote())

    def create(self):
        super().create()
        try:
            self.api().create_bucket(self.settings["bucket"])
        except Exception as exc:
            self.delete()
            raise ServiceNotUp(f"service {self.name}: its bucket: {type(exc).__name__}: {exc}") from exc
        self.parts.create(self._calls())

    def end(self, failed):
        self.parts.end(self._calls(), failed, found=self.existing)
        super().end(failed)

    def delete(self):
        self.parts.remove(self._calls(), lambda b: True, found=self.existing)
        super().delete()

    def start(self):
        if self.run_token is None:
            raise ValueError(f"service {self.name}: its container is named for the run, so it needs the run_token")
        if not has_docker():
            raise ServiceNotUp(
                f"nothing answers at {self.address}, and there is no docker here to start one; start it where "
                f"docker runs (`ducktest provision`) and set DUCKTEST_SERVICE_HOST to where it is reached from here"
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
        name = _container(self.name, run_token)
        found = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", check=False)
        return [n for n in found.stdout.split() if n == name]


S3LocalDef.instance = S3Local
