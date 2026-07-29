"""mitmproxy as a ducktest service — a forward proxy with per-request behavior injection.

Runs as a local ``mitmdump`` subprocess in the pytest venv rather than a container: the value here is the
Python addon (``mitm_addon.py``, loaded via ``-s``), which must import the same deps as the tests.
Behaviors are selected per-request by the ``x-ducktest-proxy-setting`` header.

Trust is rooted in the shared ``ca`` service — ``_spawn`` seeds mitm's confdir from that CA so every run
signs against one anchor. ``depends_on=("ca",)`` both records the relation AND orders the boot
(``provision_service`` resolves it recursively); ``_start`` still calls ``ensure_ca`` to READ the block,
since a dependent is never handed its dependency's. The mitm block then carries the seeded cert-only
anchor, so this service can adopt proxy routing and trust together.

Runtime files live in ``.ducktest/mitm/``: the seeded CA, logs, and the pidfile. Process lifecycle —
pidfile handle, leak sweep, reaping — is :mod:`._process`, shared with the other in-venv services.
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
    """The mitmdump binary, or an actionable install error. mitm runs as a SUBPROCESS, so what has to be
    present is a binary on PATH — not an importable module (contrast ca.py's guarded imports)."""
    exe = shutil.which("mitmdump")
    if exe is None:
        raise RuntimeError("mitmdump not found on PATH — install it: pip install duckdb-pytest-driver[mitm]")
    return exe


def mitm_block(**overrides):
    """Build mitm's block from an override map — one builder for boot AND attach (docs/SERVICES.md).

    ``ca_cert_file`` derives from ``confdir`` (the seeded copy there), so overriding the confdir keeps
    them consistent. An externally-run mitm passes ``endpoint=``/``ca_cert_file=`` explicitly.
    """
    port = int(overrides.get("port", _port()))
    confdir = overrides.get("confdir", "")
    endpoint = (overrides.get("endpoint") or f"http://127.0.0.1:{port}").rstrip("/")
    ca = overrides.get("ca_cert_file") or (os.path.join(confdir, _CA_NAME) if confdir else "")
    return {"port": port, "endpoint": endpoint, "confdir": confdir, "ca_cert_file": ca}


def mitm_env(block):
    """Proxy ROUTING env only — ``HTTP(S)_PROXY`` (read directly by libcurl) plus ``DUCKTEST_MITM_PROXY``
    for a body to ``SET http_proxy``. The block also carries the seeded CA anchor, so trust is adopted
    with the proxy routing.
    """
    ep = block["endpoint"]
    return {"HTTP_PROXY": ep, "HTTPS_PROXY": ep, "DUCKTEST_MITM_PROXY": ep, **ca_env(block)}


def mitm_init_sql(block, *, redact=False):
    """Suite-wide baseline preamble (`--repl` and `auto_init_sql`): route through this proxy, and tag
    each request with the running test so stateful behaviors isolate per test.

    Behaviors themselves are a per-cell concern; a cell's `init_sql` merges after this and wins. Built
    by `mitm_profile.proxy_init_sql` — one builder, since `http_extra_headers` is a single setting.
    Nothing here is secret, so `redact` changes nothing.
    """
    from .mitm_profile import proxy_init_sql

    return proxy_init_sql(endpoint=block["endpoint"])


def mitm_alive(block) -> bool:
    """A TCP connect succeeds once mitmdump is accepting — enough to gate readiness and to fail an
    ``--existing-service`` attach loud when nothing's there."""
    host = "127.0.0.1"
    port = int(block.get("port") or _port())
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# --- lifecycle: pure (confdir, port) core, so it's testable without a pytest config ----------------


def _spawn(confdir, port, ca_pem=None, ca_cert=None, behavior_modules=()):
    """Launch ``mitmdump`` as a forward proxy in ``confdir``, wait until it's accepting AND its CA exists,
    return the block. Pure ``(confdir, port)`` — no pytest config — so it's unit-testable offline.

    ``ca_pem``/``ca_cert`` are the shared CA's files (``ducktest-ca*``, ca.py), copied in under
    mitmproxy's required names BEFORE launch so mitmdump adopts our CA instead of generating its own.
    BOTH are copied: given an existing ``mitmproxy-ca.pem``, mitmdump does not re-emit the cert-only file
    that readiness waits on. ``None`` (the config-free test path) => mitmdump generates a throwaway CA.

    ``behavior_modules`` are paths registered via ``register_proxy_behaviors``; they reach the addon as
    ``--set behavior_modules=…`` so the proxy process imports the SAME files the pytest side validated.
    Without this the pytest side would accept a custom behavior name the proxy has never heard of, and
    the addon would log-and-ignore it — a test that runs with no behavior applied and passes.
    """
    exe = _mitmdump()
    if ca_pem:
        # Named, not timed: two small copies, but a mis-seeded confdir surfaces as an opaque TLS failure,
        # so the log should say which anchor this run is signing with.
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
        # Readiness is BOTH the listener and the CA file: mitmdump binds before it has written the cert
        # a client needs to trust it.
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

    _mitmdump()  # check the binary BEFORE any CA work, so a missing extra reports the direct cause
    # `depends_on=("ca",)` guarantees the CA is provisioned before this runs (provision_service resolves
    # it recursively). The direct call stays because ordering is not delivery: a dependent is never
    # handed its dependency's block, and `ensure_ca` is idempotent — so this reads it, it doesn't race.
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
