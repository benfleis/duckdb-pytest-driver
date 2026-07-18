"""Azurite (Azure Blob Storage emulator) as a ducktest service — the first shared resource.

Microsoft's official image with run-line config only (``mcr.microsoft.com/azure-storage/azurite``, public
on MCR, no ``docker login``), so no custom build. The
account/key are Azure's PUBLIC, fixed, Microsoft-published emulator credentials — **not secrets** — so
this module hardcodes them and nothing here needs ``op`` / a ``credential()``.

Boot and attach share ONE block builder (:func:`azurite_block`) so a test can't tell whether Azurite
was booted by this run or attached to an already-running one (see ``docs/SERVICES.md`` — the block/derive
contract). Provide :data:`AZURITE_SERVICE` to a suite's ``services=[...]`` and back it with a thin
session fixture that returns ``provision_service(config, AZURITE_SERVICE)``.
"""

import os
import urllib.error
import urllib.request

from ..steps import step
from ..tools.rclone import Remote
from ..suites import service
from ._docker import docker as _docker, wait_until
from ._images import ghcr_ref, register_image, served_image

# EXEMPTION (documented per code review, 2026-07-14) from AGENTS.md's "No secrets in files... use
# ${ENV_VAR} placeholders" rule: this account/key pair is NOT a secret. It's Azure's fixed, PUBLIC,
# Microsoft-published Storage Emulator credential -- identical in every Azurite install worldwide,
# published in Microsoft's own docs (e.g. https://learn.microsoft.com/azure/storage/common/storage-use-
# azurite) and reproduced verbatim across the whole ecosystem's tooling. Hardcoding it is intentional and
# correct: an ${ENV_VAR} placeholder would incorrectly imply a deployment-specific value to configure,
# when in fact using anything OTHER than this exact string is what would be wrong (it wouldn't
# authenticate against a real Azurite emulator). A secret-scanner may still flag this line; that's a
# false positive, not a policy violation -- this comment is the record of that call for future reviewers.
ACCOUNT = "devstoreaccount1"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="

# Supply chain (docs/PLAN.md, ducktest.resources._images): we MIRROR Microsoft's official public Azurite
# (was `duckdb/azurite:<tag>`, an org mirror that was never published -> `pull access denied`) into an
# org-controlled ghcr tag, and serve from there — so a test never rides MCR uptime/tag drift.
# `ducktest publish-images` freezes whatever UPSTREAM_IMAGE resolves to now into the pinned ghcr tag, so
# even upstream `:latest` yields a stable served image.
UPSTREAM_IMAGE = os.environ.get("DUCKTEST_AZURITE_UPSTREAM", "mcr.microsoft.com/azure-storage/azurite:latest")
PIN = os.environ.get("DUCKTEST_AZURITE_PIN", "2026-07-17")  # ghcr tag; bump when you re-mirror a newer upstream
GHCR_IMAGE = ghcr_ref("azurite", PIN)  # ghcr.io/<ns>/ducktest-azurite:<pin> — ns via DUCKTEST_IMAGE_NS
register_image("azurite", "mirror", UPSTREAM_IMAGE, PIN)
# What Azurite RUNS: follows DUCKTEST_IMAGE_SOURCE (upstream until the mirror is published, then ghcr —
# the PLAN [c] flip). Per-instance override via DUCKTEST_AZURITE_IMAGE (e.g. a moved endpoint).
IMAGE = os.environ.get("DUCKTEST_AZURITE_IMAGE", served_image("azurite", UPSTREAM_IMAGE, PIN))
CONTAINER = os.environ.get("DUCKTEST_AZURITE_CONTAINER", "ducktest-azurite")
BLOB_PORT = int(os.environ.get("DUCKTEST_AZURITE_BLOB_PORT", "10000"))
QUEUE_PORT = int(os.environ.get("DUCKTEST_AZURITE_QUEUE_PORT", "10001"))
TABLE_PORT = int(os.environ.get("DUCKTEST_AZURITE_TABLE_PORT", "10002"))
DEFAULT_CONTAINER = os.environ.get("DUCKTEST_AZURITE_DEFAULT_CONTAINER", "ducktest")
# A healthy Azurite boot is ~1s; 20s is a generous ceiling that still FAILS FAST rather than hanging.
_READY_TIMEOUT_S = int(os.environ.get("DUCKTEST_AZURITE_READY_TIMEOUT_S", "20"))


def _conn_str(b):
    """The Azure connection string — DERIVED from account/key/endpoint (recomputed every build)."""
    return (
        "DefaultEndpointsProtocol=http;"
        f"AccountName={b['account']};AccountKey={b['key']};"
        f"BlobEndpoint={b['blob_endpoint']};"
    )


def azurite_block(**overrides):
    """Build Azurite's service block from an override map — the single source of block truth.

    Empty overrides => the all-defaults block (public account/key, default endpoint). ``endpoint=URL``
    points at a moved/attached instance; ``account=``/``key=``/``port=``/``container=`` override the rest.
    ``blob_endpoint`` and ``connection_string`` are DERIVED, so overriding the endpoint keeps them
    consistent (that's why this is a function, not an exported dict — docs/SERVICES.md).
    """
    port = int(overrides.get("port", BLOB_PORT))
    account = overrides.get("account", ACCOUNT)
    key = overrides.get("key", KEY)
    endpoint = (overrides.get("endpoint") or f"http://127.0.0.1:{port}").rstrip("/")
    b = {
        "account": account,
        "key": key,
        "port": port,
        "endpoint": endpoint,
        "blob_endpoint": f"{endpoint}/{account}",
        "container": overrides.get("container", DEFAULT_CONTAINER),
    }
    b["connection_string"] = _conn_str(b)
    return b


def azurite_env(block):
    """The generic Azure-client env derived from an Azurite block — the connection string + account.

    Feed this as (part of) a suite's ``to_env`` (``use_service(AZURITE_SERVICE, to_env=…)``) so a bare
    ``.test`` body gets `${AZURE_STORAGE_CONNECTION_STRING}` etc. without a `.py` driver. A suite layers
    its own vars (e.g. ``AZ_DATA_DIR``) on top. See docs/SERVICES.md.
    """
    return {
        "AZURE_STORAGE_CONNECTION_STRING": block["connection_string"],
        "AZURE_STORAGE_ACCOUNT": block["account"],
        "AZ_STORAGE_ACCOUNT": block["account"],
    }


def azurite_alive(block):
    """Cheap, non-authenticating liveness probe: any HTTP response from the endpoint means Azurite is up.

    An unauthenticated GET to the blob endpoint returns 400 (InvalidQueryParameterValue) — which still
    proves the server is listening; only a connection error (refused / no route) means dead.
    """
    url = block.get("endpoint") or f"http://127.0.0.1:{BLOB_PORT}"
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except urllib.error.HTTPError:
        return True  # server responded (e.g. 400) => up
    except (urllib.error.URLError, OSError):
        return False


def _start(config):
    """Managed boot: run the standard Azurite image on the fixed host ports; return its block.

    ALWAYS_CREATE (``docker rm -f`` first => fresh) so a container leaked by an interrupted run can't
    wedge the port. If readiness fails after ``docker run``, tear the half-booted container back down
    before re-raising — ``_start`` raises before the block is stored, so ``_stop_services`` would never
    reach it (it only stops services whose block is in the store). Returns ``azurite_block(...)`` —
    identical shape to the attach path.
    """
    with step(f"starting Azurite ({IMAGE})"):
        _docker("rm", "-f", CONTAINER, check=False)  # force-remove any leftover
        # Re-spell the image's default CMD + `--skipApiVersionCheck`: a pinned/older Azurite build
        # otherwise rejects a newer rclone/SDK client with "API version … not supported". Harmless on
        # a dev emulator; makes the managed boot immune to client/image version skew (see docs/SERVICES.md).
        _docker(
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            f"{BLOB_PORT}:10000",
            "-p",
            f"{QUEUE_PORT}:10001",
            "-p",
            f"{TABLE_PORT}:10002",
            IMAGE,
            "azurite",
            "-l",
            "/data",
            "--blobHost",
            "0.0.0.0",
            "--queueHost",
            "0.0.0.0",
            "--tableHost",
            "0.0.0.0",
            "--skipApiVersionCheck",
        )
        try:
            block = azurite_block()
            wait_until(
                lambda: azurite_alive(block),
                _READY_TIMEOUT_S,
                lambda: f"Azurite container {CONTAINER!r} did not become ready on "
                f"{block.get('endpoint')} after {_READY_TIMEOUT_S}s (image {IMAGE}).",
            )
        except Exception:
            _docker("rm", "-f", CONTAINER, check=False)  # don't leak a half-booted container
            raise
    return block


def _stop(config):
    """Managed teardown (controller, once): stop AND remove the container (rm -f = stop + remove)."""
    with step("stopping Azurite"):
        _docker("rm", "-f", CONTAINER, check=False)


AZURITE_SERVICE = service(
    "azurite",
    start=_start,
    stop=_stop,
    attach=lambda overrides, config: azurite_block(**overrides),
    alive=azurite_alive,
    fixture="azurite",
)


# rclone remote for seeding/cleaning (docs/SERVICES.md § object-store seeding). For a LOCAL emulator on
# the default port, `use_emulator=true` is the canonical, special-char-free (inline-safe) form — rclone
# fills in the well-known account/key/endpoint itself. A MOVED/attached instance (non-default endpoint)
# can't use the emulator shortcut; build an explicit account/key/endpoint remote via `rclone_remote(block)`
# and address it through a config file (the account KEY's `/`/`==` break the inline form).
AZURITE_RCLONE = Remote("azurite", {"type": "azureblob", "use_emulator": True})


def rclone_remote(block=None):
    """The rclone :class:`Remote` for an Azurite instance.

    Default / local (no block, or the default endpoint) => the ``use_emulator`` remote (inline-safe).
    A moved instance (block with a non-default endpoint) => an explicit account/key/endpoint remote
    (config-file only — the key breaks inline).
    """
    default_ep = f"http://127.0.0.1:{BLOB_PORT}"
    if block is None or block.get("endpoint", default_ep).rstrip("/") == default_ep:
        return AZURITE_RCLONE
    return Remote(
        "azurite",
        {
            "type": "azureblob",
            "account": block.get("account", ACCOUNT),
            "key": block.get("key", KEY),
            "endpoint": block["endpoint"],
        },
    )
