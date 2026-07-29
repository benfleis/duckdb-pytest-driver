"""A plain HTTP(S) static file server as a ducktest service — httpfs's "normal http server".

The httpfs suite's non-S3 tests point duckdb at a local origin serving ordinary files: upstream CI runs
``python3 -m http.server`` and hands the tests a ``PYTHON_HTTP_SERVER_URL`` / ``PYTHON_HTTP_SERVER_DIR``
pair. The pattern is **write-back**: a ``.test`` body ``COPY``s a file into ``PYTHON_HTTP_SERVER_DIR`` and
reads it back over ``PYTHON_HTTP_SERVER_URL`` (the dir and the served docroot are the same directory).

An in-venv local subprocess, not docker; process lifecycle — pidfile handle, leak sweep, reaping — is
:mod:`._process`. Runtime files live in ``.ducktest/httpserver/``. The block carries ``use_ssl``:

- **HTTP** (default) is pure stdlib — ``python -m http.server`` — no CA, no ``[mitm]`` dep, and it matches
  upstream.
- **HTTPS** (``DUCKTEST_HTTP_SERVER_TLS=1``) exists because real httpfs traffic is TLS. The stdlib CLI has
  no TLS flag, so a bundled runner (``_tls_server.py``) serves it, and — an *origin* needs a concrete cert,
  unlike a proxy — the shared CA code mints a ``127.0.0.1``/``localhost`` leaf. The TLS path adopts the
  CA trust env itself, so plain HTTP does not require or provision the CA.
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
# The stdlib TLS runner, spawned via `sys.executable` for the HTTPS path (the plain path uses
# `-m http.server`, which has no TLS flag). Sits next to this module, addressed by path.
_TLS_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tls_server.py")

# Fixed default port, safe because `start` sweeps a leaked process off it via the pidfile.
_DEFAULT_PORT = int(os.environ.get("DUCKTEST_HTTP_SERVER_PORT", "18008"))
_READY_TIMEOUT_S = int(os.environ.get("DUCKTEST_HTTP_SERVER_READY_TIMEOUT_S", "15"))


def _port() -> int:
    return int(os.environ.get("DUCKTEST_HTTP_SERVER_PORT", _DEFAULT_PORT))


def _use_ssl() -> bool:
    return os.environ.get("DUCKTEST_HTTP_SERVER_TLS", "0").lower() in ("1", "true")


def http_block(**overrides):
    """Build the service block from an override map — the single source of block truth (boot AND attach).
    Empty overrides => the all-defaults block.

    ``endpoint`` and ``use_ssl`` are kept CONSISTENT: an explicit ``endpoint`` (attach) is authoritative and
    ``use_ssl`` is derived from its scheme; otherwise the endpoint is built from ``use_ssl`` on the local
    loopback. ``docroot`` is both the served root and the ``PYTHON_HTTP_SERVER_DIR`` a body writes into.
    """
    port = int(overrides.get("port", _port()))
    if overrides.get("endpoint"):
        endpoint = overrides["endpoint"].rstrip("/")
        use_ssl = endpoint.startswith("https://")
    else:
        use_ssl = bool(overrides.get("use_ssl", _use_ssl()))
        endpoint = f"{'https' if use_ssl else 'http'}://127.0.0.1:{port}"
    return {
        "port": port,
        "endpoint": endpoint,
        "docroot": overrides.get("docroot", ""),
        "use_ssl": use_ssl,
        "ca_cert_file": overrides.get("ca_cert_file", ""),
    }


def http_env(block):
    """The env a suite adopts so a ``.test`` body reaches the server. Emits the httpfs contract pair
    (``PYTHON_HTTP_SERVER_URL`` = the origin, ``PYTHON_HTTP_SERVER_DIR`` = the writable docroot). The URL
    scheme already reflects ``use_ssl``; the HTTPS block also carries the CA trust anchor."""
    env = {"PYTHON_HTTP_SERVER_URL": block["endpoint"]}
    if block.get("docroot"):
        env["PYTHON_HTTP_SERVER_DIR"] = block["docroot"]
    if block.get("use_ssl"):
        env.update(ca_env(block))
    return env


def http_alive(block) -> bool:
    """Liveness probe: a plain TCP connect to the listen port succeeds once the server is accepting.

    Cheap and non-authenticating (mitm's stance) — enough to gate readiness and to fail an
    ``--existing-service`` attach loud when nothing's there. For the TLS server this connects at the TCP
    layer before the handshake, which is all readiness needs."""
    port = int(block.get("port") or _port())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


# --- lifecycle: pure (control_dir, docroot, port) core, so it's testable without a pytest config ---


def _reset_docroot(docroot) -> None:
    """Clean slate for the served/write dir. Unlike the CA (reused across runs, trusted once), write-back
    content must NOT linger: a deleted test's file left in the docroot would still be served."""
    if os.path.isdir(docroot):
        shutil.rmtree(docroot)
    os.makedirs(docroot, exist_ok=True)


def _spawn(control_dir, docroot, port, use_ssl=False, certfile=None, keyfile=None, ca_cert_file=""):
    """Launch the file server on ``docroot``, wait until it's accepting, return the block. Pure
    ``(control_dir, docroot, port, …)`` — no pytest config — so it's unit-testable offline.

    HTTP uses stdlib ``-m http.server``; HTTPS runs the bundled ``_tls_server`` with a leaf ``certfile`` /
    ``keyfile`` minted by the caller from the shared CA. Runtime files live in ``control_dir``, kept OUT of
    ``docroot`` so they are never served.
    """
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
        # Mint this origin's leaf from the shared CA BEFORE boot. This is intentionally conditional:
        # plain HTTP must not require cryptography or generate/adopt a CA.
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
