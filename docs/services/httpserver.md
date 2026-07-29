# `httpserver` — the static-file origin

`ducktest.resources.httpserver` · key `httpserver` · fixture `httpserver` · **in-venv subprocess**

Managed equivalent of httpfs's `python3 -m http.server` fixture. It exports
`PYTHON_HTTP_SERVER_URL` and `PYTHON_HTTP_SERVER_DIR`.

## The write-back pattern

The served docroot and `PYTHON_HTTP_SERVER_DIR` are the same directory:

```sql
COPY (SELECT 42 AS a) TO '${PYTHON_HTTP_SERVER_DIR}/x.csv';
SELECT * FROM '${PYTHON_HTTP_SERVER_URL}/x.csv';
```

Each boot resets the docroot.

## Block

| field | |
|---|---|
| `port` | listen port (default `18008`) |
| `endpoint` | `http(s)://127.0.0.1:<port>` |
| `docroot` | served root and writable dir |
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

The bundled runner wraps `ThreadingHTTPServer` in an `SSLContext` and takes
`<port> <docroot> <certfile> <keyfile>`.

## Lifecycle

Process lifecycle is [`_process.py`](README.md#the-in-venv-subprocess-pattern), shared with
[`mitmproxy`](mitmproxy.md). Runtime files live in `.ducktest/httpserver/`, outside the served `www/` dir.

`alive` is a TCP connect to the listen port.

## Knobs

| env var | default | |
|---|---|---|
| `DUCKTEST_HTTP_SERVER_PORT` | `18008` | fixed port; the pidfile sweep makes that safe |
| `DUCKTEST_HTTP_SERVER_TLS` | `0` | `1`/`true` → HTTPS |
| `DUCKTEST_HTTP_SERVER_READY_TIMEOUT_S` | `15` | |
