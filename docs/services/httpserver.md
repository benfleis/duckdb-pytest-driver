# `httpserver` — the static-file origin

`ducktest.resources.httpserver` · key `httpserver` · fixture `httpserver` · **in-venv subprocess**

httpfs's "normal http server". Upstream CI runs `python3 -m http.server` and hands the tests a
`PYTHON_HTTP_SERVER_URL` / `PYTHON_HTTP_SERVER_DIR` pair; this is that, as a managed service.

## The write-back pattern

The served docroot and `PYTHON_HTTP_SERVER_DIR` are the same directory. A body writes a file and reads it
back over the URL:

```sql
COPY (SELECT 42 AS a) TO '${PYTHON_HTTP_SERVER_DIR}/x.csv';
SELECT * FROM '${PYTHON_HTTP_SERVER_URL}/x.csv';
```

Each boot resets the docroot — a deleted test's leftover file would otherwise still be served.

## Block

| field | |
|---|---|
| `port` | listen port (default `18008`) |
| `endpoint` | `http(s)://127.0.0.1:<port>` |
| `docroot` | served root **and** the writable dir a body targets |
| `use_ssl` | whether the origin speaks TLS |
| `ca_cert_file` | HTTPS only: the CA anchor used by the minted leaf |

An explicit `endpoint` (attach) is authoritative and `use_ssl` derives from its scheme; otherwise the
endpoint is built from `use_ssl`.

## Env

| var | value |
|---|---|
| `PYTHON_HTTP_SERVER_URL` | `endpoint` |
| `PYTHON_HTTP_SERVER_DIR` | `docroot` (omitted if unset) |

HTTPS trust is included by this service when `use_ssl` is enabled. Plain HTTP does not provision or
require the CA.

## HTTP vs HTTPS

| | HTTP (default) | HTTPS (`DUCKTEST_HTTP_SERVER_TLS=1`) |
|---|---|---|
| runner | `python -m http.server` | bundled `_tls_server.py` |
| deps | none | `[tls]` extra (cryptography, for the CA) |
| cert | — | a `127.0.0.1`/`localhost` leaf minted from the shared CA |

HTTPS exists because real httpfs traffic is TLS. The stdlib CLI has no TLS flag and mitm is a proxy rather
than an origin, hence the bundled runner: it wraps `ThreadingHTTPServer` in an `SSLContext` and takes
`<port> <docroot> <certfile> <keyfile>` on argv.

## Lifecycle

Process lifecycle is [`_process.py`](README.md#the-in-venv-subprocess-pattern), shared with
[`mitmproxy`](mitmproxy.md). Identity is `Proc("httpserver", "http server")`, so runtime files are
`httpserver.pid` / `httpserver.log` in `.ducktest/httpserver/` — kept out of `www/` so they're never served.

This service's own parts: the docroot reset, the HTTP-vs-TLS command, and `alive` as a plain TCP connect
(for TLS that lands below the handshake, which is all readiness needs).

## Knobs

| env var | default | |
|---|---|---|
| `DUCKTEST_HTTP_SERVER_PORT` | `18008` | fixed port; the pidfile sweep makes that safe |
| `DUCKTEST_HTTP_SERVER_TLS` | `0` | `1`/`true` → HTTPS |
| `DUCKTEST_HTTP_SERVER_READY_TIMEOUT_S` | `15` | |
