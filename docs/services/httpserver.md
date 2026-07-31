# `httpserver` — the file origin

`ducktest.resources.httpserver` · key `httpserver` · fixture `httpserver` · **in-venv subprocess**

Managed equivalent of `python3 -m http.server`, with stable shared read data and a distinct writable
location for every SQLLogic subprocess.

## Data and temp

Populate stable read data once under the block's `data_dir`. The service exports matching stable
URL/local roots, and a suite maps them into the unittest binary's DATA/TEMP properties:

```python
{
    "data_dir": env_ref("HTTP_DATA_URL"),
    "local_data_dir": env_ref("HTTP_DATA_DIR"),
    "temp_dir_root": env_ref("HTTP_TEMP_URL_ROOT"),
    "local_temp_dir_root": env_ref("HTTP_TEMP_DIR_ROOT"),
}
```

The binary appends the same session/batch/test suffix to both TEMP roots. A body writes
`{LOCAL_TEMP_DIR}/x.csv` and reads the exact file through `{TEMP_DIR}/x.csv`. Each boot resets the
whole docroot.

## Block

| field | |
|---|---|
| `port` | listen port (default `18008`) |
| `endpoint` | `http(s)://127.0.0.1:<port>` |
| `docroot` | served root |
| `data_dir` / `data_url` | stable shared read location |
| `temp_dir_root` / `temp_url_root` | parents for isolated writable locations |
| `use_ssl` | whether the origin speaks TLS |
| `ca_cert_file` | HTTPS only: the CA anchor used by the minted leaf |

An explicit `endpoint` (attach) is authoritative and `use_ssl` derives from its scheme; otherwise the
endpoint is built from `use_ssl`.

## Env

| var | value |
|---|---|
| `HTTP_DATA_URL` | stable `<endpoint>/data` |
| `HTTP_DATA_DIR` | matching stable `<docroot>/data` |
| `HTTP_TEMP_URL_ROOT` | stable `<endpoint>/temp` root |
| `HTTP_TEMP_DIR_ROOT` | matching stable `<docroot>/temp` root |

HTTPS trust is included by this service when `use_ssl` is enabled. Plain HTTP does not provision or
require the CA. Local root variables are omitted when an attached origin has no `docroot`; an
endpoint alone cannot establish the local-to-URL mapping.

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
