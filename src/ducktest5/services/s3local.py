"""A local S3-compatible server, as a Service: run in docker, or found running at an address.

It runs as RustFS (`DUCKTEST_S3_LOCAL_IMAGE` overrides the image). A server that already answers where
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

from ..host import docker, has_docker, in_url, service_bind, service_host
from ..resources import ALWAYS, NEVER, ON_FAILURE, S3CredentialDef, Service, ServiceDef, ServiceNotUp, StorageDef
from ..resources.remote import S3Access, remote_for
from .s3api import S3Api

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
class Bucket:
    """A bucket the server holds while the run uses it: `seed` copies each local directory to its
    prefix, `public` opens it to anonymous list and read. A bucket the run made goes at the end,
    unless `retain` keeps it; it defaults to `never`, as a kept one would be used by the next run with
    whatever the tests wrote into it."""

    name: str
    seed: tuple = ()  # ((local directory, prefix in the bucket), ...)
    public: bool = False
    retain: str = NEVER

    def content(self):
        return {"name": self.name, "seed": [list(s) for s in self.seed], "public": self.public, "retain": self.retain}


def bucket(name: str, seed=(), public: bool = False, retain: str = NEVER) -> Bucket:
    """A bucket on a local S3 server: `s3_local(buckets=[bucket("data", seed=[("dir", "prefix")])])`."""
    return Bucket(name, tuple(tuple(s) for s in seed), public, retain)


@dataclass(eq=False)
class S3LocalDef(ServiceDef):
    """A local S3-compatible server with its bucket made: one that already answers where the run
    reaches it (DUCKTEST_SERVICE_HOST, DUCKTEST_S3_LOCAL_PORT), else in a docker container named for the
    run, or the one `.as_existing(address=...)` names. It runs as RustFS. Its attributes are
    `endpoint`, `host_port` (the endpoint without its scheme), `bucket` and `region`; its keys come
    from the environment (DUCKTEST_S3_LOCAL_ACCESS_KEY, DUCKTEST_S3_LOCAL_SECRET_KEY) and reach a test
    through `credential(id, scopes=[...])`. `storage(id, prefix)` declares a storage in its bucket.

    `buckets` are more buckets, each a `bucket(...)`, made when the server is and filled from local
    directories. The run owns a bucket it made, and removes it at the end. One that is already there
    and holds what it seeds (and is public when asked) is used as it is and never changed; one with
    other content is refused, and the service is not up. `bucket` itself is made when it is not
    there, and never removed."""

    kind = "s3_local"
    detached = True
    attributes = ("endpoint", "host_port", "bucket", "region")
    secret_attributes = ("access_key", "secret_key")
    credential_type = S3CredentialDef
    delivers = {"key_id": "access_key", "secret": "secret_key", "region": "region"}

    id: str = "s3"
    bucket: str = "ducktest"
    buckets: list = field(default_factory=list)
    port: int | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE

    def reached_at(self, address: str) -> S3LocalDef:
        # Cleanup after the run reaches the server for its storages; the buckets the run made are gone.
        return dataclasses.replace(self.as_existing(address), buckets=[])

    def storage(self, id: str, prefix: str = "", bucket: str | None = None, **fields) -> StorageDef:
        """A storage at `s3://<bucket>/<prefix>` on this server; `bucket` names one of `buckets`, else
        it is the server's own."""
        if bucket is not None and bucket not in [b.name for b in self.buckets]:
            raise ValueError(f"service {self.id}: {bucket!r} is not one of its buckets")
        root = f"s3://{bucket or '{' + self.id + '.bucket}'}/{prefix}".rstrip("/")
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
        self.made_buckets = []  # names of the buckets of `buckets` this instance made, in order

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

    def create(self):
        super().create()
        api = self.api()
        try:
            missing = [d for b in self.definition.buckets for d, _ in b.seed if not os.path.isdir(d)]
            if missing:
                raise ServiceNotUp(f"service {self.id}: nothing to seed from: no directory at {', '.join(missing)}")
            api.create_bucket(self.settings["bucket"])
            for b in self.definition.buckets:
                self._bucket(api, b)
        except Exception as exc:
            self._remove_buckets(api, lambda b: True)
            self.delete()
            if isinstance(exc, ServiceNotUp):
                raise
            raise ServiceNotUp(f"service {self.id}: its buckets: {type(exc).__name__}: {exc}") from exc

    def _differs(self, api, b):
        """What in bucket `b` is not as declared, or None: it is."""
        for source, prefix in b.seed:
            uri = f"s3://{b.name}/{prefix}".rstrip("/")
            if not self.remote().check(source, uri):
                return f"{uri} does not hold what is in {source}"
        if b.public and not api.is_public(b.name):
            return f"s3://{b.name} is not open to anonymous reads"
        return None

    def _bucket(self, api, b):
        if b.name not in self.made_buckets and api.bucket_exists(b.name):
            if differs := self._differs(api, b):
                raise ServiceNotUp(
                    f"service {self.id}: bucket {b.name} is there and {differs}; the run does not change a "
                    f"bucket it did not make. Remove it, or declare another bucket"
                )
            _log.info("s3: bucket %s is there as declared, used as it is", b.name)
            return
        if b.name in self.made_buckets and not self._differs(api, b):
            return
        _log.info("s3: making bucket %s", b.name)
        api.create_bucket(b.name)
        if b.name not in self.made_buckets:
            self.made_buckets.append(b.name)
        for source, prefix in b.seed:
            self.remote().copy(source, f"s3://{b.name}/{prefix}".rstrip("/"))
        if b.public:
            api.make_public(b.name)

    def _remove_buckets(self, api, goes):
        """Remove each bucket this instance made for which `goes(bucket)`, the last made first."""
        by_name = {b.name: b for b in self.definition.buckets}
        for name in reversed(list(self.made_buckets)):
            if goes(by_name[name]):
                _log.info("s3: removing bucket %s, made by the run", name)
                api.delete_bucket(name, force=True)
                self.made_buckets.remove(name)

    def end(self, failed):
        self._remove_buckets(self.api(), lambda b: not _retained(b.retain, failed))
        super().end(failed)

    def delete(self):
        self._remove_buckets(self.api(), lambda b: True)
        super().delete()

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


def _retained(policy, failed):
    return policy == ALWAYS or (policy == ON_FAILURE and failed)


S3LocalDef.instance = S3Local
