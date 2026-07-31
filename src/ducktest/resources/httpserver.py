"""HTTP(S) file origin with stable DATA/TEMP roots for the unittest binary.

HTTP uses ``python -m http.server``. HTTPS uses ``_tls_server.py`` with a leaf certificate minted from the
shared CA. Plain HTTP does not provision the CA.
"""

import os
import shutil
import socket
import sys

from ..suites import service
from ..workspace import scratch
from . import _process as proc
from .ca import ca_env, ensure_ca, leaf_cert

PROC = proc.Proc("httpserver", "http server")  # -> httpserver.pid / httpserver.log in the control dir
# The stdlib CLI has no TLS flag, so HTTPS uses this local runner.
_TLS_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tls_server.py")

# Fixed default port, safe because `start` sweeps a leaked process off it via the pidfile.
_DEFAULT_PORT = int(os.environ.get("DUCKTEST_HTTP_SERVER_PORT", "18008"))
_READY_TIMEOUT_S = int(os.environ.get("DUCKTEST_HTTP_SERVER_READY_TIMEOUT_S", "15"))


def _port() -> int:
    return int(os.environ.get("DUCKTEST_HTTP_SERVER_PORT", _DEFAULT_PORT))


def _use_ssl() -> bool:
    return os.environ.get("DUCKTEST_HTTP_SERVER_TLS", "0").lower() in ("1", "true")


def http_block(**overrides):
    """Build the service block for managed and attached instances."""
    port = int(overrides.get("port", _port()))
    if overrides.get("endpoint"):
        endpoint = overrides["endpoint"].rstrip("/")
        use_ssl = endpoint.startswith("https://")
    else:
        use_ssl = bool(overrides.get("use_ssl", _use_ssl()))
        endpoint = f"{'https' if use_ssl else 'http'}://127.0.0.1:{port}"
    docroot = overrides.get("docroot", "")
    return {
        "port": port,
        "endpoint": endpoint,
        "docroot": docroot,
        "data_dir": overrides.get("data_dir") or (os.path.join(docroot, "data") if docroot else ""),
        "data_url": (overrides.get("data_url") or f"{endpoint}/data").rstrip("/"),
        "temp_dir_root": overrides.get("temp_dir_root") or (os.path.join(docroot, "temp") if docroot else ""),
        "temp_url_root": (overrides.get("temp_url_root") or f"{endpoint}/temp").rstrip("/"),
        "use_ssl": use_ssl,
        "ca_cert_file": overrides.get("ca_cert_file", ""),
    }


def http_env(block):
    """Stable DATA and TEMP roots shared by every invocation using this origin."""
    env = {
        "HTTP_DATA_URL": block["data_url"],
        "HTTP_TEMP_URL_ROOT": block["temp_url_root"],
    }
    if block.get("data_dir"):
        env["HTTP_DATA_DIR"] = block["data_dir"]
    if block.get("temp_dir_root"):
        env["HTTP_TEMP_DIR_ROOT"] = block["temp_dir_root"]
    if block.get("use_ssl"):
        env.update(ca_env(block))
    return env


def http_alive(block) -> bool:
    """Liveness probe: TCP connect to the listen port."""
    port = int(block.get("port") or _port())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


# --- lifecycle: pure (control_dir, docroot, port) core, so it's testable without a pytest config ---


def _reset_docroot(docroot) -> None:
    """Clean the served/write directory before each boot."""
    if os.path.isdir(docroot):
        shutil.rmtree(docroot)
    os.makedirs(os.path.join(docroot, "data"), exist_ok=True)
    os.makedirs(os.path.join(docroot, "temp"), exist_ok=True)


def _spawn(control_dir, docroot, port, use_ssl=False, certfile=None, keyfile=None, ca_cert_file=""):
    """Launch the file server and return its block."""
    _reset_docroot(docroot)
    block = http_block(port=port, use_ssl=use_ssl, docroot=docroot, ca_cert_file=ca_cert_file)
    if use_ssl:
        cmd = [sys.executable, _TLS_SERVER, str(port), docroot, certfile, keyfile]
    else:
        cmd = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", docroot]
    proc.launch(
        PROC,
        control_dir,
        cmd,
        ready=lambda: http_alive(block),
        timeout_s=_READY_TIMEOUT_S,
        where=f"127.0.0.1:{port}",
        detail=f"{'https' if use_ssl else 'http'} :{port}, docroot={docroot}",
    )
    return block


def _shutdown(control_dir) -> None:
    proc.shutdown(PROC, control_dir)


# --- config-wrapped entry points the service() descriptor calls ------------------------------------


def _start(config):
    control_dir = scratch(config, "httpserver")
    docroot = scratch(config, "httpserver", "www")
    certfile = keyfile = None
    ca_cert_file = ""
    use_ssl = _use_ssl()
    if use_ssl:
        ca = ensure_ca(config)
        leaf = leaf_cert(ca, "127.0.0.1", ["127.0.0.1", "localhost"], control_dir)
        certfile, keyfile = leaf["certfile"], leaf["keyfile"]
        ca_cert_file = ca["ca_cert_file"]
    return _spawn(
        control_dir,
        docroot,
        _port(),
        use_ssl=use_ssl,
        certfile=certfile,
        keyfile=keyfile,
        ca_cert_file=ca_cert_file,
    )


def _stop(config):
    _shutdown(scratch(config, "httpserver"))


HTTPSERVER_SERVICE = service(
    "httpserver",
    start=_start,
    stop=_stop,
    attach=lambda overrides, config: http_block(**overrides),
    alive=http_alive,
    fixture="httpserver",
    to_env=http_env,
)
