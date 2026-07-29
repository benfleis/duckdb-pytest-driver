# `ca` — the shared trust anchor

`ducktest.resources.ca` · key `ca` · **no process** · needs `[tls]` to generate

Shared certificate authority for local TLS services. Each TLS service gets its own leaf certificate
signed by this CA; clients trust `ca_cert_file`.

It has no process. `start` generates the CA, `stop` is `None`, and `alive` checks the cert file. `to_env`
exports trust settings for bare `.test` files.

## Block

| field | |
|---|---|
| `dir` | the CA directory (`.ducktest/ca/`) |
| `ca_pem` | `<dir>/ducktest-ca.pem` — key + cert for signing |
| `ca_cert_file` | `<dir>/ducktest-ca-cert.pem` — cert-only trust anchor |

Both paths derive from `dir`.

## Env

| var | for |
|---|---|
| `CURL_CA_BUNDLE` | libcurl reads it directly — the curl client trusts the anchor automatically |
| `DUCKTEST_CA_CERT` | the httplib client needs the setting: `SET ca_cert_file='${DUCKTEST_CA_CERT}'` |

## Generation

`ensure_ca(config)` generates once into `.ducktest/ca/` and is idempotent.

The CA is built with `cryptography`. `ducktest-ca.pem` contains key + cert because mitmproxy reads both
from one PEM file; `ducktest-ca-cert.pem` contains only the cert.

## Minting a leaf

A TLS origin needs a concrete cert before it starts:

```python
leaf = leaf_cert(ca_block, "127.0.0.1", ["127.0.0.1", "localhost"], dest_dir)
# -> {"certfile": …, "keyfile": …}  — feed to ssl.SSLContext.load_cert_chain
```

Each SAN string becomes an IP or DNS `GeneralName`.

## Dependencies on a base install

`cryptography` is the optional `[tls]` extra. Pure block/env helpers and plain HTTP imports still work
without it; `_generate_ca` and `leaf_cert` fail with:

```
TLS provisioning needs cryptography (No module named 'cryptography') —
install it: pip install duckdb-pytest-driver[tls]
```

`[tls]` has no extra Python-version floor. `[mitm]` includes `cryptography` and is gated by mitmproxy's
Python requirements.

## Consumers

| service | how it uses the CA |
|---|---|
| [`mitmproxy`](mitmproxy.md) | copies `ducktest-ca*` into its confdir as `mitmproxy-ca*`, so it signs with the shared anchor instead of generating its own |
| [`httpserver`](httpserver.md) (TLS) | conditionally mints a `127.0.0.1`/`localhost` leaf via `leaf_cert` |

`mitmproxy` declares `depends_on=("ca",)`, so the CA is provisioned before mitm starts. It still calls
`ensure_ca(config)` in `_start` to read the CA block.

`httpserver` is conditional: plain HTTP does not provision the CA, while HTTPS generates the CA and
includes the trust env through the `httpserver` block.
