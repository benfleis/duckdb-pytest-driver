"""Shared certificate authority for local TLS services.

Each TLS server gets its own leaf certificate signed by this CA. Clients trust the cert-only
``ca_cert_file``.

The service has no process: ``start`` generates the CA, ``stop`` is ``None``, and ``alive`` checks the
cert file. ``to_env`` exports trust settings for bare ``.test`` files.

Files are named by role: ``ducktest-ca.pem`` contains key + cert for signing, and
``ducktest-ca-cert.pem`` is the trust anchor.
"""

import datetime
import ipaddress
import os

from ..steps import step
from ..suites import service
from ..workspace import scratch

# Guarded because httpserver imports this module for its plain-HTTP path, which must work on a base
# install; `_require_deps` then reports the missing extra at the point of use.
try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError as exc:
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

_BASENAME = "ducktest"
_CA_PEM = f"{_BASENAME}-ca.pem"  # private key + cert for signing
_CA_CERT = f"{_BASENAME}-ca-cert.pem"  # cert only — the anchor duckdb's `ca_cert_file` points at
_KEY_SIZE = 2048
_CA_DAYS = 3650
_LEAF_DAYS = 825  # clients (Apple's included) reject server certs valid materially longer


def _require_deps():
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            f"TLS provisioning needs cryptography ({_IMPORT_ERROR}) — install it: pip install duckdb-pytest-driver[tls]"
        ) from _IMPORT_ERROR


def ca_block(**overrides):
    """Build the CA block from an override map (boot AND attach). ``ca_pem``/``ca_cert_file`` derive from
    ``dir``, so pointing at a different dir keeps them consistent."""
    cadir = overrides.get("dir", "")
    return {
        "dir": cadir,
        "ca_pem": overrides.get("ca_pem") or (os.path.join(cadir, _CA_PEM) if cadir else ""),
        "ca_cert_file": overrides.get("ca_cert_file") or (os.path.join(cadir, _CA_CERT) if cadir else ""),
    }


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)


def _key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _validity(days):
    """Backdated a day so a client whose clock trails ours doesn't see a not-yet-valid cert."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(days=1), now + datetime.timedelta(days=days)


def _write(path, data, *, private=False):
    """A key-bearing file is created 0600 rather than chmod'd after, so it is never briefly readable."""
    if private:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    else:
        with open(path, "wb") as f:
            f.write(data)


def _san(name):
    """An IP literal becomes an ``IPAddress`` SAN, anything else a ``DNSName``."""
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def _create_ca(cadir, block):
    key = _new_key()
    who = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, _BASENAME),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, _BASENAME),
        ]
    )
    start, end = _validity(_CA_DAYS)
    cert = (
        x509.CertificateBuilder()
        .subject_name(who)
        .issuer_name(who)  # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    os.makedirs(cadir, exist_ok=True)
    # mitmproxy expects key + cert concatenated in one PEM file.
    _write(block["ca_pem"], _key_pem(key) + cert_pem, private=True)
    _write(block["ca_cert_file"], cert_pem)


def _generate_ca(cadir):
    """Generate the CA into ``cadir`` unless it already exists."""
    block = ca_block(dir=cadir)
    if not os.path.exists(block["ca_pem"]):
        _require_deps()
        with step(f"generating shared CA in {cadir}"):
            _create_ca(cadir, block)
    return block


def ensure_ca(config):
    """Provision the shared CA into ``.ducktest/ca/`` and return its block."""
    return _generate_ca(scratch(config, "ca"))


def _load_ca(block):
    """The CA's signing key (from ``ca_pem``) and its cert (from the cert-only file)."""
    with open(block["ca_pem"], "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    with open(block["ca_cert_file"], "rb") as f:
        return key, x509.load_pem_x509_certificate(f.read())


def leaf_cert(ca_block, common_name, sans, dest_dir, *, basename="server"):
    """Mint a leaf cert + key signed by the shared CA into ``dest_dir``; return both paths.

    Returns ``{"certfile", "keyfile"}`` — feed straight to ``ssl.SSLContext.load_cert_chain``.
    """
    _require_deps()
    certfile = os.path.join(dest_dir, f"{basename}-cert.pem")
    keyfile = os.path.join(dest_dir, f"{basename}-key.pem")
    with step(f"minting leaf cert for {common_name}"):
        ca_key, ca_cert = _load_ca(ca_block)
        key = _new_key()
        start, end = _validity(_LEAF_DAYS)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            # Hostname matching is SAN-only in modern clients; the CN above is cosmetic.
            .add_extension(x509.SubjectAlternativeName([_san(s) for s in sans]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        os.makedirs(dest_dir, exist_ok=True)
        _write(certfile, cert.public_bytes(serialization.Encoding.PEM))
        _write(keyfile, _key_pem(key), private=True)
    return {"certfile": certfile, "keyfile": keyfile}


def ca_alive(block) -> bool:
    return bool(block.get("ca_cert_file")) and os.path.exists(block["ca_cert_file"])


def ca_env(block):
    """Trust env: ``CURL_CA_BUNDLE`` (libcurl reads it directly) + ``DUCKTEST_CA_CERT`` (the httplib client
    needs the setting, so a body does ``SET ca_cert_file='${DUCKTEST_CA_CERT}'``)."""
    cert = block.get("ca_cert_file")
    return {"CURL_CA_BUNDLE": cert, "DUCKTEST_CA_CERT": cert} if cert else {}


CA_SERVICE = service(
    "ca",
    start=ensure_ca,
    stop=None,  # nothing to tear down — the CA persists and is reused
    attach=lambda overrides, config: ca_block(**overrides),
    alive=ca_alive,
    to_env=ca_env,
)
