"""A local Azure blob server, as a Service: run in docker, or found running at an address.

`flavor` names what backs it; Azurite is the only one (`DUCKTEST_AZ_LOCAL_IMAGE` overrides its
image). It serves blob only: no DFS endpoint, so no abfss://. A server that already answers where
the run reaches it is used as it is, so a machine with no docker runs against one started elsewhere.
A test does not see the server directly; it sees the container, the account, and the credential
the service produces.

The account key is that of a local server, not a secret, but it reaches a test only through a
credential all the same, because an existing instance may be anyone's.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..binary import UsageError
from ..host import docker, has_docker, in_url, service_bind, service_host
from ..resources import ALWAYS, NEVER, ON_FAILURE, AzureCredentialDef, Service, ServiceDef, ServiceNotUp, StorageDef
from ..resources.remote import AzureBlobAccess, remote_for
from .azblob import PUBLIC_LEVELS, BlobApi

_log = logging.getLogger(__name__)


AZURITE_IMAGE = "mcr.microsoft.com/azure-storage/azurite:3.37.0"
# Not a secret: the storage emulator's fixed account and key, the same in every Azurite, published by
# Microsoft (https://learn.microsoft.com/azure/storage/common/storage-use-azurite). Any other value
# would not authenticate against a default Azurite.
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
FLAVORS = ("azurite",)


def az_settings(**overrides):
    """How one instance is reached and started; `DUCKTEST_AZ_LOCAL_*` of the environment give the defaults."""
    port = int(overrides.get("port", os.environ.get("DUCKTEST_AZ_LOCAL_PORT", "10000")))
    bind = overrides.get("bind", service_bind())
    host = overrides.get("host", service_host())
    address = overrides.get("address", f"http://{in_url(host)}:{port}").rstrip("/")
    account = overrides.get("account", os.environ.get("DUCKTEST_AZ_LOCAL_ACCOUNT", AZURITE_ACCOUNT))
    key = overrides.get("key", os.environ.get("DUCKTEST_AZ_LOCAL_KEY", AZURITE_KEY))
    endpoint = f"{address}/{account}"
    protocol = address.split("://", 1)[0]
    return {
        "address": address,
        "endpoint": endpoint,
        "account": account,
        "key": key,
        "connection_string": (
            f"DefaultEndpointsProtocol={protocol};AccountName={account};AccountKey={key};BlobEndpoint={endpoint};"
        ),
        "container": overrides.get("container", os.environ.get("DUCKTEST_AZ_LOCAL_CONTAINER", "ducktest")),
        "docker_name": overrides["docker_name"],
        "port": port,
        "publish": f"{in_url(bind)}:{port}",
    }


def az_answers(endpoint):
    """Whether a Blob service answers at `endpoint`. It refuses an unsigned listing, and that refusal
    is the answer; a 5xx is one that is not ready yet."""
    try:
        with urllib.request.urlopen(f"{endpoint}?comp=list", timeout=3):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


def _start(settings):
    # A container leaked by an interrupted run holds the port; `rm -f` succeeding here means one was found.
    if docker("rm", "-f", settings["docker_name"], check=False).returncode == 0:
        _log.warning("az: removing a container left by an earlier run: %s", settings["docker_name"])
    accounts = []
    if (settings["account"], settings["key"]) != (AZURITE_ACCOUNT, AZURITE_KEY):
        accounts = ["-e", f"AZURITE_ACCOUNTS={settings['account']}:{settings['key']}"]
    docker(
        "run", "-d", "--name", settings["docker_name"],
        "-p", f"{settings['publish']}:10000",
        *accounts,
        os.environ.get("DUCKTEST_AZ_LOCAL_IMAGE", AZURITE_IMAGE),
        # Blob only. A pinned Azurite refuses a newer SDK's API version unless told not to check it.
        "azurite-blob", "--blobHost", "0.0.0.0", "--blobPort", "10000", "--location", "/data",
        "--skipApiVersionCheck",
    )  # fmt: skip


@dataclass(frozen=True)
class Container:
    """A container the server holds while the run uses it: `seed` copies each local directory to its
    prefix, `public` opens it to anonymous reads (True or 'container': list and read; 'blob': read). A
    container the run made goes at the end, unless `retain` keeps it; it defaults to `never`, as a
    kept one would be used by the next run with whatever the tests wrote into it."""

    name: str
    seed: tuple = ()  # ((local directory, prefix in the container), ...)
    public: str | None = None
    retain: str = NEVER

    def content(self):
        return {"name": self.name, "seed": [list(s) for s in self.seed], "public": self.public, "retain": self.retain}


def container(name: str, seed=(), public: bool | str = False, retain: str = NEVER) -> Container:
    """A container on a local Azure blob server: `az_local(containers=[container("data", seed=[("dir", "")])])`."""
    level = "container" if public is True else (public or None)
    if level is not None and level not in PUBLIC_LEVELS:
        raise UsageError(f"container {name!r}: public={public!r}; it is True, False, or one of {PUBLIC_LEVELS}")
    return Container(name, tuple(tuple(s) for s in seed), level, retain)


@dataclass(eq=False)
class AZLocalDef(ServiceDef):
    """A local Azure blob server with its container made: one that already answers where the run
    reaches it (DUCKTEST_SERVICE_HOST, DUCKTEST_AZ_LOCAL_PORT), else in a docker container named for
    the run, or the one `.as_existing(address="http://HOST:PORT")` names. `flavor` is what backs it:
    `azurite`. Its attributes are `endpoint` (the blob service URL, with the account), `account` and
    `container`; its key comes from the environment (DUCKTEST_AZ_LOCAL_ACCOUNT, DUCKTEST_AZ_LOCAL_KEY;
    Azurite's own account when unset) and reaches a test through `credential(id, scopes=[...])`, an
    AzureCredentialDef of its connection string. `storage(id, prefix)` declares a storage in its
    container, `az://<container>/<prefix>`.

    `containers` are more containers, each a `container(...)`, made when the server is and filled
    from local directories. The run owns a container it made, and removes it at the end. One that is
    already there and holds what it seeds (and is public as asked) is used as it is and never
    changed; one with other content is refused, and the service is not up. `container` itself is
    made when it is not there, and never removed."""

    kind = "az_local"
    detached = True
    attributes = ("endpoint", "account", "container")
    secret_attributes = ("key", "connection_string")
    credential_type = AzureCredentialDef
    delivers = {"connection_string": "connection_string", "account_name": "account"}

    id: str = "az"
    flavor: str = "azurite"
    container: str = "ducktest"
    containers: list = field(default_factory=list)
    port: int | None = None
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE

    def __post_init__(self):
        if self.flavor not in FLAVORS:
            raise UsageError(f"service {self.id!r}: flavor {self.flavor!r}; it is one of {', '.join(FLAVORS)}")

    def reached_at(self, address: str) -> AZLocalDef:
        # Cleanup after the run reaches the server for its storages; the containers the run made are gone.
        return dataclasses.replace(self.as_existing(address), containers=[])

    def check_storage(self, storage) -> None:
        scheme = storage.root.split("://", 1)[0].lower() if "://" in storage.root else "file"
        if "{" not in scheme and scheme != "az":
            raise UsageError(
                f"storage {storage.id!r} ({storage.root}) is on service {self.id!r} ({self.flavor}), which "
                "serves blob storage only, as az://; it has no DFS endpoint for abfss://"
            )

    def storage(self, id: str, prefix: str = "", container: str | None = None, **fields) -> StorageDef:
        """A storage at `az://<container>/<prefix>` on this server; `container` names one of
        `containers`, else it is the server's own."""
        if container is not None and container not in [c.name for c in self.containers]:
            raise ValueError(f"service {self.id}: {container!r} is not one of its containers")
        root = f"az://{container or '{' + self.id + '.container}'}/{prefix}".rstrip("/")
        return StorageDef(id, root, account=f"{{{self.id}.account}}", inputs=[self], **fields)


def az_local(id: str = "az", **fields) -> AZLocalDef:
    """A local Azure blob server; see AZLocalDef."""
    return AZLocalDef(id, **fields)


def _docker_name(service_id, run_token):
    return f"ducktest-{service_id}-{run_token}"


class AZLocal(Service):
    """A stopped container is kept when `retain` keeps it; dealloc removes it. One found running is
    never touched: no docker call is made for it."""

    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        overrides = {"container": definition.container, "docker_name": _docker_name(definition.id, run_token)}
        if definition.port is not None:
            overrides["port"] = definition.port
        if definition.address is not None:
            overrides["address"] = definition.address
        self.settings = az_settings(**overrides)
        self.made_containers = []  # names of the `containers` this instance made, in order

    @property
    def handle(self):
        return self.settings["docker_name"]

    @property
    def address(self):
        return self.settings["address"]

    def _key(self):
        return self.settings["key"]

    def remote(self):
        """The file operations on this server's store (rclone), with its own key."""
        return remote_for(AzureBlobAccess(self.settings["endpoint"], self.settings["account"], self._key))

    def api(self):
        """The server's own container calls: Blob service requests signed with the account key."""
        return BlobApi(self.settings["endpoint"], self.settings["account"], self._key)

    def create(self):
        super().create()
        api = self.api()
        try:
            missing = [d for c in self.definition.containers for d, _ in c.seed if not os.path.isdir(d)]
            if missing:
                raise ServiceNotUp(f"service {self.id}: nothing to seed from: no directory at {', '.join(missing)}")
            api.create_container(self.settings["container"])
            for c in self.definition.containers:
                self._container(api, c)
        except Exception as exc:
            self._remove_containers(api, lambda c: True)
            self.delete()
            if isinstance(exc, ServiceNotUp):
                raise
            raise ServiceNotUp(f"service {self.id}: its containers: {type(exc).__name__}: {exc}") from exc

    def _differs(self, api, c):
        """What in container `c` is not as declared, or None: it is."""
        for source, prefix in c.seed:
            uri = f"az://{c.name}/{prefix}".rstrip("/")
            if not self.remote().check(source, uri):
                return f"{uri} does not hold what is in {source}"
        if c.public and api.public_access(c.name) != c.public:
            return f"az://{c.name} is not open to anonymous reads of each {c.public}"
        return None

    def _container(self, api, c):
        if c.name not in self.made_containers and api.container_exists(c.name):
            if differs := self._differs(api, c):
                raise ServiceNotUp(
                    f"service {self.id}: container {c.name} is there and {differs}; the run does not change a "
                    f"container it did not make. Remove it, or declare another container"
                )
            _log.info("az: container %s is there as declared, used as it is", c.name)
            return
        if c.name in self.made_containers and not self._differs(api, c):
            return
        _log.info("az: making container %s", c.name)
        api.create_container(c.name)
        if c.name not in self.made_containers:
            self.made_containers.append(c.name)
        for source, prefix in c.seed:
            self.remote().copy(source, f"az://{c.name}/{prefix}".rstrip("/"))
        if c.public:
            api.set_public_access(c.name, c.public)

    def _remove_containers(self, api, goes):
        """Remove each container this instance made for which `goes(container)`, the last made first."""
        by_name = {c.name: c for c in self.definition.containers}
        for name in reversed(list(self.made_containers)):
            if goes(by_name[name]):
                _log.info("az: removing container %s, made by the run", name)
                api.delete_container(name)
                self.made_containers.remove(name)

    def end(self, failed):
        self._remove_containers(self.api(), lambda c: not _retained(c.retain, failed))
        super().end(failed)

    def delete(self):
        self._remove_containers(self.api(), lambda c: True)
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
        return az_answers(self.settings["endpoint"])

    answers = ready

    def list(self, run_token):
        if self.existing:
            return []
        name = _docker_name(self.id, run_token)
        found = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", check=False)
        return [n for n in found.stdout.split() if n == name]


def _retained(policy, failed):
    return policy == ALWAYS or (policy == ON_FAILURE and failed)


AZLocalDef.instance = AZLocal
