"""mitmproxy service with per-request behavior injection.

Runs ``mitmdump`` as a local subprocess and loads ``mitm_addon.py`` via ``-s``. Behaviors are selected by
the ``x-ducktest-proxy-setting`` header.

``depends_on=("ca",)`` orders CA provisioning before launch. ``_start`` reads the CA block with
``ensure_ca(config)`` and seeds mitmproxy's confdir. ``mitm_env`` exports both proxy routing and trust.

Runtime files live in ``.ducktest/mitm/``.
"""

import os
import shutil
import socket

from ..steps import step
from ..suites import service
from ..workspace import scratch
from . import _process as proc
from .ca import ca_env, ensure_ca

# mitmproxy's required confdir filenames: `_CA_NAME` (cert only) is what a client trusts, `_CA_PEM`
# (key+cert) is what mitm signs with.
_CA_NAME = "mitmproxy-ca-cert.pem"
_CA_PEM = "mitmproxy-ca.pem"
PROC = proc.Proc("mitm", "mitmproxy")  # -> mitm.pid / mitm.log in the confdir
# Addressed by path, not import: mitmdump loads a script file. See mitm_addon.py's import-clean contract.
_ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mitm_addon.py")

# Fixed default port, safe because `start` sweeps a leaked process off it via the pidfile.
_DEFAULT_PORT = int(os.environ.get("DUCKTEST_MITM_PORT", "18080"))
_READY_TIMEOUT_S = int(os.environ.get("DUCKTEST_MITM_READY_TIMEOUT_S", "15"))


def _port() -> int:
    return int(os.environ.get("DUCKTEST_MITM_PORT", _DEFAULT_PORT))


def _mitmdump() -> str:
    """Return the mitmdump binary path, or raise an install hint."""
    exe = shutil.which("mitmdump")
    if exe is None:
        raise RuntimeError("mitmdump not found on PATH — install it: pip install duckdb-pytest-driver[mitm]")
    return exe


def mitm_block(**overrides):
    """Build mitm's block for managed and attached instances."""
    port = int(overrides.get("port", _port()))
    confdir = overrides.get("confdir", "")
    endpoint = (overrides.get("endpoint") or f"http://127.0.0.1:{port}").rstrip("/")
    ca = overrides.get("ca_cert_file") or (os.path.join(confdir, _CA_NAME) if confdir else "")
    return {"port": port, "endpoint": endpoint, "confdir": confdir, "ca_cert_file": ca}


def mitm_env(block):
    """Environment for proxy routing and trust."""
    ep = block["endpoint"]
    return {"HTTP_PROXY": ep, "HTTPS_PROXY": ep, "DUCKTEST_MITM_PROXY": ep, **ca_env(block)}


def mitm_init_sql(block, *, redact=False):
    """Suite-wide proxy preamble for ``--repl`` and ``auto_init_sql``."""
    from .mitm_profile import proxy_init_sql

    return proxy_init_sql(endpoint=block["endpoint"])


def mitm_alive(block) -> bool:
    """Liveness probe: TCP connect to the listen port."""
    host = "127.0.0.1"
    port = int(block.get("port") or _port())
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# --- lifecycle: pure (confdir, port) core, so it's testable without a pytest config ----------------


def _spawn(confdir, port, ca_pem=None, ca_cert=None, behavior_modules=()):
    """Launch ``mitmdump`` and return its block."""
    exe = _mitmdump()
    if ca_pem:
        os.makedirs(confdir, exist_ok=True)
        with step(f"seeding mitmproxy CA from {os.path.dirname(ca_pem)}"):
            shutil.copyfile(ca_pem, os.path.join(confdir, _CA_PEM))
            if ca_cert and os.path.exists(ca_cert):
                shutil.copyfile(ca_cert, os.path.join(confdir, _CA_NAME))
    block = mitm_block(port=port, confdir=confdir)
    cmd = [
        exe,
        "-s",
        _ADDON,
        "--set",
        f"confdir={confdir}",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "-q",
    ]
    if behavior_modules:
        cmd += ["--set", "behavior_modules=" + ",".join(str(p) for p in behavior_modules)]
    proc.launch(
        PROC,
        confdir,
        cmd,
        # mitmdump binds before the cert-only CA file exists.
        ready=lambda: mitm_alive(block) and os.path.exists(block["ca_cert_file"]),
        timeout_s=_READY_TIMEOUT_S,
        where=f"127.0.0.1:{port}",
        detail=f":{port}, confdir={confdir}",
    )
    return block


def _shutdown(confdir) -> None:
    proc.shutdown(PROC, confdir)


# --- config-wrapped entry points the service() descriptor calls ------------------------------------


def _start(config):
    from .mitm_profile import registered_behavior_modules

    _mitmdump()  # fail before CA work if mitmdump is missing
    ca = ensure_ca(config)
    return _spawn(
        scratch(config, "mitm"),
        _port(),
        ca_pem=ca["ca_pem"],
        ca_cert=ca["ca_cert_file"],
        behavior_modules=registered_behavior_modules(),
    )


def _stop(config):
    _shutdown(scratch(config, "mitm"))


MITM_SERVICE = service(
    "mitmproxy",
    start=_start,
    stop=_stop,
    attach=lambda overrides, config: mitm_block(**overrides),
    alive=mitm_alive,
    fixture="mitmproxy",
    depends_on=("ca",),
    to_env=mitm_env,
    to_init_sql=mitm_init_sql,
)
