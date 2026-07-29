"""Offline self-test for the in-venv mitmproxy service (Stage 0: plumbing + CA trust).

Boots a REAL ``mitmdump`` subprocess — but that's a local process in this venv, so unlike the docker
resource tier this is genuinely offline (no docker, no network) and runs in the default suite. It
drives the pure ``(confdir, port)`` lifecycle (``_spawn``/``_shutdown``) directly, so no pytest config
/ SessionContext is needed; the config-wrapped ``_start``/``_stop`` are thin ``scratch(...)`` wrappers
over these. Proves: the proxy comes up + is alive, the CA lands in the confdir (what ``ca_cert_file``
points at), the block/env shapes are right, and teardown actually kills it.
"""

import os
import shutil
import socket

import pytest
from cryptography import x509

from ducktest.resources import _process as proc
from ducktest.resources import mitmproxy as mitm

# The `[mitm]` extra brings mitmdump; skip cleanly where it isn't installed rather than hard-fail.
pytestmark = pytest.mark.skipif(shutil.which("mitmdump") is None, reason="mitmdump not installed ([mitm] extra)")


def _free_port() -> int:
    """An ephemeral port the OS just handed us — closed immediately, so mitmdump can bind it. A tiny
    reuse race is acceptable in a test (and avoids colliding with a real run on the fixed default)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_block_and_env_shapes():
    """Pure block/env derivation — no process. mitm's env carries proxy routing and trust together."""
    block = mitm.mitm_block(port=12345, confdir="/tmp/x")
    assert block["endpoint"] == "http://127.0.0.1:12345"
    assert block["ca_cert_file"] == "/tmp/x/mitmproxy-ca-cert.pem"

    env = mitm.mitm_env(block)
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == env["DUCKTEST_MITM_PROXY"] == "http://127.0.0.1:12345"
    assert env["CURL_CA_BUNDLE"] == env["DUCKTEST_CA_CERT"] == "/tmp/x/mitmproxy-ca-cert.pem"


def test_declares_depends_on_ca():
    assert mitm.MITM_SERVICE.depends_on == ("ca",)


def test_missing_mitmdump_reports_the_binary_not_the_ca(monkeypatch, tmp_path):
    """mitm needs a BINARY on PATH, so that's what it checks. `_start` checks it before doing any CA work:
    a base install asked to boot a proxy should read 'mitmdump not found', not a TLS/crypto message."""
    monkeypatch.setattr(mitm.shutil, "which", lambda _: None)
    monkeypatch.setattr(mitm, "scratch", lambda config, *parts: str(tmp_path))

    def _no_ca(config):
        raise AssertionError("_start reached the CA before checking for mitmdump")

    monkeypatch.setattr(mitm, "ensure_ca", _no_ca)

    with pytest.raises(RuntimeError, match=r"mitmdump not found on PATH"):
        mitm._start(object())


def test_seeds_confdir_from_shared_ca(tmp_path):
    """mitm boots with the SHARED CA seeded into its confdir (byte-identical), so it signs against the
    one trust anchor rather than generating its own."""
    from ducktest.resources import ca

    cablock = ca._generate_ca(str(tmp_path / "ca"))
    confdir = str(tmp_path / "mitm")
    block = mitm._spawn(confdir, _free_port(), ca_pem=cablock["ca_pem"], ca_cert=cablock["ca_cert_file"])
    try:
        # the generic ducktest-ca.pem, copied in under mitmproxy's required confdir name
        seeded = os.path.join(confdir, "mitmproxy-ca.pem")
        assert mitm.mitm_alive(block)
        with open(seeded, "rb") as a, open(cablock["ca_pem"], "rb") as b:
            assert a.read() == b.read()  # mitm's signing CA IS the shared one
    finally:
        mitm._shutdown(confdir)


def test_mitmproxy_can_sign_with_our_ca(tmp_path):
    """The CA is ours (ca.py builds it with `cryptography`), but mitmproxy has to be able to ADOPT it —
    load the key+cert out of the one seeded `mitmproxy-ca.pem` and mint interception certs from it. Asserted
    through mitmproxy's own CertStore, which is exactly what mitmdump uses, so this pins the on-disk format
    contract that copying the file byte-for-byte does not.
    """
    import pathlib

    from mitmproxy.certs import CertStore

    from ducktest.resources import ca

    cablock = ca._generate_ca(str(tmp_path / "ca"))
    confdir = tmp_path / "mitm"
    confdir.mkdir()
    shutil.copyfile(cablock["ca_pem"], confdir / "mitmproxy-ca.pem")

    store = CertStore.from_store(pathlib.Path(confdir), "mitmproxy", 2048)
    entry = store.get_cert("example.test", [], "mitmproxy")

    assert entry.cert.cn == "example.test"
    # The minted leaf must chain to OUR anchor, not one mitmproxy generated for itself.
    with open(cablock["ca_cert_file"], "rb") as f:
        anchor = x509.load_pem_x509_certificate(f.read())
    assert entry.cert.to_cryptography().issuer == anchor.subject


def test_lifecycle(tmp_path):
    """Boot → alive + CA present → stop → dead. The whole Stage 0 contract."""
    confdir = str(tmp_path)
    port = _free_port()

    assert not mitm.mitm_alive({"port": port})  # nothing there yet

    block = mitm._spawn(confdir, port)
    try:
        assert block["port"] == port
        assert mitm.mitm_alive(block)
        assert os.path.exists(block["ca_cert_file"]), "mitmproxy should generate its CA into the confdir"
        assert os.path.exists(proc.pidfile(mitm.PROC, confdir))
    finally:
        mitm._shutdown(confdir)

    assert not mitm.mitm_alive(block), "proxy should be down after shutdown"
    assert not os.path.exists(proc.pidfile(mitm.PROC, confdir)), "pidfile should be swept on shutdown"


def test_spawn_sweeps_a_stale_process(tmp_path):
    """A second spawn on the same confdir sweeps the first (an interrupted run must not wedge the port)."""
    confdir = str(tmp_path)
    port = _free_port()

    mitm._spawn(confdir, port)
    first_pid = proc.read_pid(mitm.PROC, confdir)
    try:
        second = mitm._spawn(confdir, port)  # same confdir + port -> must reclaim it
        second_pid = proc.read_pid(mitm.PROC, confdir)
        assert second_pid != first_pid
        assert mitm.mitm_alive(second)
    finally:
        mitm._shutdown(confdir)
