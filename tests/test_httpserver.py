"""Offline tests for the in-venv httpserver service."""

import os
import socket
import ssl
import urllib.request

import pytest

from ducktest.resources import _process as proc
from ducktest.resources import ca
from ducktest.resources import httpserver as hs

_HAVE_TLS = ca._IMPORT_ERROR is None


def _free_port() -> int:
    """Return an ephemeral port for the test server."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_block_and_env_shapes():
    """Pure block/env derivation — no process. Scheme tracks use_ssl; env carries the httpfs contract pair."""
    http = hs.http_block(port=12345, docroot="/tmp/www")
    assert http["endpoint"] == "http://127.0.0.1:12345"
    assert http["use_ssl"] is False
    assert http["ca_cert_file"] == ""

    https = hs.http_block(port=443, docroot="/tmp/www", use_ssl=True, ca_cert_file="/tmp/ca.pem")
    assert https["endpoint"] == "https://127.0.0.1:443"
    assert https["use_ssl"] is True
    assert https["ca_cert_file"] == "/tmp/ca.pem"

    # an explicit endpoint (attach) is authoritative — use_ssl is derived from its scheme
    attached = hs.http_block(endpoint="https://example.test:9", docroot="/d", ca_cert_file="/tmp/attached.pem")
    assert attached["use_ssl"] is True

    env = hs.http_env(http)
    assert env["PYTHON_HTTP_SERVER_URL"] == "http://127.0.0.1:12345"
    assert env["PYTHON_HTTP_SERVER_DIR"] == "/tmp/www"
    assert "CURL_CA_BUNDLE" not in env
    assert "PYTHON_HTTP_SERVER_DIR" not in hs.http_env(hs.http_block(port=1))  # no docroot -> no dir var

    tls_env = hs.http_env(https)
    assert tls_env["PYTHON_HTTP_SERVER_URL"] == "https://127.0.0.1:443"
    assert tls_env["CURL_CA_BUNDLE"] == tls_env["DUCKTEST_CA_CERT"] == "/tmp/ca.pem"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", False),
        ("on", False),
        ("0", False),
        ("false", False),
    ],
)
def test_tls_env_knob_accepts_only_true_and_one(monkeypatch, value, expected):
    monkeypatch.setenv("DUCKTEST_HTTP_SERVER_TLS", value)
    assert hs._use_ssl() is expected


def test_service_has_no_static_ca_dependency():
    assert hs.HTTPSERVER_SERVICE.depends_on == ()


def test_start_plain_http_does_not_touch_ca(monkeypatch, tmp_path):
    """Plain HTTP must stay dependency-free: no cryptography, no CA generation, no trust env."""
    monkeypatch.setenv("DUCKTEST_HTTP_SERVER_TLS", "0")
    monkeypatch.setattr(hs, "scratch", lambda config, *parts: str(tmp_path.joinpath(*parts)))

    def _no_ca(config):
        raise AssertionError("_start reached the CA for plain HTTP")

    def _fake_spawn(control_dir, docroot, port, use_ssl=False, certfile=None, keyfile=None, ca_cert_file=""):
        assert use_ssl is False
        assert ca_cert_file == ""
        return hs.http_block(port=port, use_ssl=use_ssl, docroot=docroot, ca_cert_file=ca_cert_file)

    monkeypatch.setattr(hs, "ensure_ca", _no_ca)
    monkeypatch.setattr(hs, "_spawn", _fake_spawn)

    block = hs._start(object())
    assert block["use_ssl"] is False
    assert "CURL_CA_BUNDLE" not in hs.http_env(block)


def test_start_https_adopts_ca_trust_through_http_block(monkeypatch, tmp_path):
    monkeypatch.setenv("DUCKTEST_HTTP_SERVER_TLS", "1")
    monkeypatch.setattr(hs, "scratch", lambda config, *parts: str(tmp_path.joinpath(*parts)))
    cablock = {"ca_pem": str(tmp_path / "ducktest-ca.pem"), "ca_cert_file": str(tmp_path / "ducktest-ca-cert.pem")}
    leaf = {"certfile": str(tmp_path / "server-cert.pem"), "keyfile": str(tmp_path / "server-key.pem")}

    def _fake_spawn(control_dir, docroot, port, use_ssl=False, certfile=None, keyfile=None, ca_cert_file=""):
        assert use_ssl is True
        assert certfile == leaf["certfile"]
        assert keyfile == leaf["keyfile"]
        assert ca_cert_file == cablock["ca_cert_file"]
        return hs.http_block(port=port, use_ssl=use_ssl, docroot=docroot, ca_cert_file=ca_cert_file)

    monkeypatch.setattr(hs, "ensure_ca", lambda config: cablock)
    monkeypatch.setattr(hs, "leaf_cert", lambda ca_block, common_name, sans, dest_dir: leaf)
    monkeypatch.setattr(hs, "_spawn", _fake_spawn)

    env = hs.http_env(hs._start(object()))
    assert env["CURL_CA_BUNDLE"] == env["DUCKTEST_CA_CERT"] == cablock["ca_cert_file"]


def test_lifecycle_http(tmp_path):
    """Boot, serve a file written after startup, then stop."""
    control, docroot = str(tmp_path / "ctl"), str(tmp_path / "www")
    port = _free_port()

    assert not hs.http_alive({"port": port})  # nothing there yet

    block = hs._spawn(control, docroot, port)
    try:
        assert block["port"] == port and block["use_ssl"] is False
        assert hs.http_alive(block)
        assert os.path.exists(proc.pidfile(hs.PROC, control))

        # Write after boot, matching a `.test` body that materializes files with COPY.
        with open(os.path.join(docroot, "hello.csv"), "w") as f:
            f.write("a\n1\n")
        with urllib.request.urlopen(f"{block['endpoint']}/hello.csv", timeout=5) as r:
            assert r.read() == b"a\n1\n"
    finally:
        hs._shutdown(control)

    assert not hs.http_alive(block), "server should be down after shutdown"
    assert not os.path.exists(proc.pidfile(hs.PROC, control)), "pidfile should be swept on shutdown"


def test_reset_docroot_clears_stale(tmp_path):
    """A boot resets the docroot: a prior run's leftover file must not survive into the served tree."""
    control, docroot = str(tmp_path / "ctl"), str(tmp_path / "www")
    os.makedirs(docroot)
    with open(os.path.join(docroot, "stale.txt"), "w") as f:
        f.write("old")

    hs._spawn(control, docroot, _free_port())
    try:
        assert not os.path.exists(os.path.join(docroot, "stale.txt"))
    finally:
        hs._shutdown(control)


def test_spawn_sweeps_a_stale_process(tmp_path):
    """A second spawn on the same control dir + port sweeps the first (an interrupted run must not wedge it)."""
    control, docroot = str(tmp_path / "ctl"), str(tmp_path / "www")
    port = _free_port()

    hs._spawn(control, docroot, port)
    first_pid = proc.read_pid(hs.PROC, control)
    try:
        second = hs._spawn(control, docroot, port)  # same control dir + port -> must reclaim it
        assert proc.read_pid(hs.PROC, control) != first_pid
        assert hs.http_alive(second)
    finally:
        hs._shutdown(control)


@pytest.mark.skipif(not _HAVE_TLS, reason="cryptography not installed ([tls] extra) — CA leaf minting")
def test_lifecycle_https(tmp_path):
    """HTTPS origin: mint a leaf from the shared CA, serve TLS, fetch over https trusting the CA anchor."""
    cadir = str(tmp_path / "ca")
    control, docroot = str(tmp_path / "ctl"), str(tmp_path / "www")
    port = _free_port()

    ca_block = ca._generate_ca(cadir)
    leaf = ca.leaf_cert(ca_block, "127.0.0.1", ["127.0.0.1", "localhost"], control)

    block = hs._spawn(control, docroot, port, use_ssl=True, certfile=leaf["certfile"], keyfile=leaf["keyfile"])
    try:
        assert block["endpoint"] == f"https://127.0.0.1:{port}"
        with open(os.path.join(docroot, "hello.csv"), "w") as f:
            f.write("b\n2\n")
        # Trust only the shared CA, proving the leaf chains to the exported anchor.
        ctx = ssl.create_default_context(cafile=ca_block["ca_cert_file"])
        with urllib.request.urlopen(f"{block['endpoint']}/hello.csv", context=ctx, timeout=5) as r:
            assert r.read() == b"b\n2\n"
    finally:
        hs._shutdown(control)
