# `ca` — the shared trust anchor

`ducktest.resources.ca` · key `ca` · **no process** · needs `[tls]` to generate

The one certificate authority every TLS service in a run signs against. What must be shared is the CA, not
a cert: each TLS server gets its own leaf signed by this anchor, duckdb trusts the single `ca_cert_file`,
so every leaf is trusted.

It has no process — `start` generates, `stop` is `None`, `alive` is "the cert file exists". It's a service
only to get **`to_env`**, which adopts the trust vars into `os.environ` pre-fork so a bare `.test` inherits
them with no `.py` driver.

## Block

| field | |
|---|---|
| `dir` | the CA directory (`.ducktest/ca/`) |
| `ca_pem` | `<dir>/ducktest-ca.pem` — key + cert; what a proxy **signs** with |
| `ca_cert_file` | `<dir>/ducktest-ca-cert.pem` — cert only; the **trust anchor** |

Both paths derive from `dir`. Files are named generically rather than after a consumer — mitm renames its
copies to `mitmproxy-ca*`.

## Env

| var | for |
|---|---|
| `CURL_CA_BUNDLE` | libcurl reads it directly — the curl client trusts the anchor automatically |
| `DUCKTEST_CA_CERT` | the httplib client needs the setting: `SET ca_cert_file='${DUCKTEST_CA_CERT}'` |

## Generation

`ensure_ca(config)` generates once into `.ducktest/ca/` and is idempotent. The scratch dir is stable
across runs, so the anchor is trusted once and kept rather than regenerated per session.

Built with `cryptography` directly — a self-signed CA (`BasicConstraints(ca=True)`, `KeyUsage` with
`key_cert_sign`, SKI) written as key PEM + cert PEM concatenated in `ducktest-ca.pem`, plus the cert alone
in `ducktest-ca-cert.pem`. That concatenated layout is what mitmproxy reads both halves out of when it
adopts the CA, so it is a format contract, not an implementation detail — `test_mitmproxy_can_sign_with_our_ca`
pins it via mitmproxy's own loader.

## Minting a leaf

A proxy mints leaves per connection; a TLS **origin** needs a concrete cert up front:

```python
leaf = leaf_cert(ca_block, "127.0.0.1", ["127.0.0.1", "localhost"], dest_dir)
# -> {"certfile": …, "keyfile": …}  — feed to ssl.SSLContext.load_cert_chain
```

Each SAN string becomes an IP or DNS `GeneralName`. The leaf chains to the anchor `ca_cert_file` points
at, so duckdb trusts it with no extra config.

## Dependencies on a base install

`cryptography` is the optional `[tls]` extra, but [`httpserver`](httpserver.md) imports this module at
module scope and its **plain-HTTP path must work without it**. So the imports are at the top of the file
behind a guard, and `_generate_ca` / `leaf_cert` call `_require_deps()` first:

```
TLS provisioning needs cryptography (No module named 'cryptography') —
install it: pip install duckdb-pytest-driver[tls]
```

The original `ImportError` is chained as `__cause__`. `ca_block`, `ca_env`, `ca_alive` and the plain-HTTP
server keep working.

`[tls]` carries **no python floor** — only `[mitm]` does, because mitmproxy needs 3.12+. So the CA and
httpserver's HTTPS mode work on the driver's own 3.9 floor; only the proxy drops out. `[mitm]` includes
`cryptography`, since mitm signs with this same CA. [`mitmproxy`](mitmproxy.md) needs the `mitmdump`
**binary** rather than an importable module, so it checks `shutil.which` instead.

## Consumers

| service | how it uses the CA |
|---|---|
| [`mitmproxy`](mitmproxy.md) | copies `ducktest-ca*` into its confdir as `mitmproxy-ca*`, so it signs with the shared anchor instead of generating its own |
| [`httpserver`](httpserver.md) (TLS) | conditionally mints a `127.0.0.1`/`localhost` leaf via `leaf_cert` |

`mitmproxy` declares `depends_on=("ca",)`, which orders the boot — `provision_service` provisions
dependencies recursively (with cycle detection) before the dependent's `start`. It still calls
`ensure_ca(config)` from its own `_start`, because ordering is not delivery: a dependent is never handed
its dependency's block, so the call is how the block is *read*. Generation is idempotent, so repeated
callers are safe.

`httpserver` is conditional: plain HTTP does not provision the CA, while HTTPS generates the CA and
includes the trust env through the `httpserver` block.
