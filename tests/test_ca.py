"""Self-test for the shared CA resource (ca.py). Generation needs the ``[tls]`` extra (cryptography); the
pure block/env shapes need nothing."""

import os

import pytest

from ducktest.resources import ca

_HAVE_TLS = ca._IMPORT_ERROR is None


def test_block_and_env_shapes():
    """Pure derivation — no generation. Paths derive from `dir`; env carries the trust anchor twice."""
    block = ca.ca_block(dir="/tmp/x")
    assert block["ca_pem"].endswith("/ducktest-ca.pem")  # generic, not mitmproxy-specific
    assert block["ca_cert_file"].endswith("/ducktest-ca-cert.pem")

    env = ca.ca_env(block)
    assert env["CURL_CA_BUNDLE"] == env["DUCKTEST_CA_CERT"] == block["ca_cert_file"]
    assert ca.ca_env(ca.ca_block()) == {}  # no dir -> no cert -> no env


def test_service_shape():
    assert ca.CA_SERVICE.key == "ca"
    assert ca.CA_SERVICE.stop is None  # nothing to tear down


@pytest.mark.skipif(not _HAVE_TLS, reason="cryptography not installed ([tls] extra)")
def test_generate_once_and_reuse(tmp_path):
    """Generates a real CA, and a second call REUSES it (generate-once / stable anchor)."""
    b1 = ca._generate_ca(str(tmp_path))
    assert os.path.exists(b1["ca_pem"]) and os.path.exists(b1["ca_cert_file"])
    assert ca.ca_alive(b1)
    mtime = os.path.getmtime(b1["ca_pem"])

    b2 = ca._generate_ca(str(tmp_path))  # idempotent: must NOT regenerate
    assert os.path.getmtime(b2["ca_pem"]) == mtime
    assert b2["ca_cert_file"] == b1["ca_cert_file"]


def test_alive_false_when_absent(tmp_path):
    assert not ca.ca_alive(ca.ca_block(dir=str(tmp_path / "nonexistent")))


def test_missing_extra_gives_an_actionable_error(monkeypatch, tmp_path):
    """On a base install cryptography is absent. Importing this module must still work (httpserver's
    plain-HTTP path depends on that), and the crypto entry points must say what to install rather than
    dying on a NameError mid-mint."""
    monkeypatch.setattr(ca, "_IMPORT_ERROR", ImportError("No module named 'cryptography'"))

    for call in (
        lambda: ca._generate_ca(str(tmp_path / "fresh")),
        lambda: ca.leaf_cert(ca.ca_block(dir=str(tmp_path)), "host", [], str(tmp_path)),
    ):
        with pytest.raises(RuntimeError, match=r"pip install duckdb-pytest-driver\[tls\]") as err:
            call()
        assert isinstance(err.value.__cause__, ImportError)  # the real cause stays attached

    # Pure derivation must NOT require the extra.
    assert ca.ca_block(dir="/tmp/x")["ca_cert_file"].endswith("ducktest-ca-cert.pem")
