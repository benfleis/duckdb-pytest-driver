# `mitmproxy` — L7 fault injection + capture

`ducktest.resources.mitmproxy` · key `mitmproxy` · fixture `mitmproxy` · **in-venv subprocess** ·
`[mitm]` extra

A forward proxy for duckdb HTTP traffic. A Python addon applies per-request behaviors selected by header.

Runs as a local `mitmdump` subprocess so behavior modules share the pytest venv.

## Block

| field | |
|---|---|
| `port` | listen port (default `18080`) |
| `endpoint` | `http://127.0.0.1:<port>` — the forward-proxy URL |
| `confdir` | `.ducktest/mitm/` — seeded CA, logs, pidfile |
| `ca_cert_file` | derived: `<confdir>/mitmproxy-ca-cert.pem` |

## Env

| var | |
|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` | the standard pair; libcurl reads these directly |
| `DUCKTEST_MITM_PROXY` | for a body to `SET http_proxy='${DUCKTEST_MITM_PROXY}'` |
| `CURL_CA_BUNDLE` / `DUCKTEST_CA_CERT` | the seeded mitm CA anchor |

The service depends on [`CA_SERVICE`](ca.md) for boot ordering. `mitm_env` exports both routing and trust.

## CA seeding

Before launch, `_spawn` copies the shared CA into the confdir under mitmproxy's required names:

| shared (`.ducktest/ca/`) | seeded (`confdir`) |
|---|---|
| `ducktest-ca.pem` | `mitmproxy-ca.pem` |
| `ducktest-ca-cert.pem` | `mitmproxy-ca-cert.pem` |

Both files are copied; readiness waits for the cert-only file.

## Lifecycle

Process lifecycle is [`_process.py`](README.md#the-in-venv-subprocess-pattern), shared with
[`httpserver`](httpserver.md). Runtime files are `mitm.pid` / `mitm.log` in the confdir.

Readiness waits for the listener and the CA file. `_start` checks for `mitmdump` before CA work, so a
missing `[mitm]` install reports the proxy dependency first.

| env var | default |
|---|---|
| `DUCKTEST_MITM_PORT` | `18080` |
| `DUCKTEST_MITM_READY_TIMEOUT_S` | `15` |

## The behavior dispatcher (`mitm_addon.py`)

Loaded into mitmdump via `-s`, by file path.

> **Import-clean by contract.** The addon runs inside the mitmdump process. It may import only
> `mitmproxy` and the stdlib. Behavior modules follow the same rule.

### Selection headers

duckdb carries these via the `extra_http_headers` setting, written by `proxy_init_sql` into the
`--init-sqllogic` preamble. Composed with `map_concat(current_setting(...), ...)`, not replaced.

| header | |
|---|---|
| `x-ducktest-proxy-setting` | comma-list of `name[;k=v;…]`, applied in order — e.g. `latency;ms=500, fail_nth;n=1` |
| `x-ducktest-test` | per-test **identity tag**: keys per-`(tag, name)` state, seeds a reproducible per-`(tag, name)` RNG, and will be the capture-attribution key |

No setting header → pass-through. A request-phase behavior that sets `flow.response` short-circuits the
rest of the request-phase chain.

### Built-in behaviors

| name | params | |
|---|---|---|
| `latency` | `ms=200` | delay the response (response phase, async) |
| `status` | `code=503` | short-circuit with a status; upstream is never hit |
| `fail_nth` | `n=1`, `status=503` | fail the first *n* requests **per tag**, then recover — exercises retry logic |
| `require_auth` | `username=`, `password=` (optional, both or neither) | bare: 407 unless `Proxy-Authorization` is present; with both set: 407 unless the decoded Basic credentials match |

`require_auth` models proxy authentication. Credential values may not contain `,`/`;`/`=` (`_check_token`).

### Writing a behavior

A behavior is a **class**: params declared in `__init__`, effect in `__call__(self, flow, ctx)` (sync or
async — the dispatcher awaits an awaitable result). Subclass `ProxyBehavior`.

```python
from ducktest.resources.mitm_addon import ProxyBehavior


class teapot(ProxyBehavior):
    def __init__(self, code=418):
        self.code = int(code)              # coerce: wire params arrive as strings

    def __call__(self, flow, ctx):
        from mitmproxy import http         # deferred — keeps the module stdlib-importable

        if ctx.phase == "request":
            flow.response = http.Response.make(self.code)


BEHAVIORS = {"teapot": teapot}
```

The signature is the schema. Unknown keywords raise `TypeError` at the call site. The class name is the
wire name.

Conventions, each enforced:

- **One attribute per param, same name** — `to_str()` reads the signature and those attributes.
- **`__init__` coerces and nothing else.** It runs in pytest with Python values and in mitmdump with
  strings from the wire.
- **State goes in `ctx.state`**, not on the instance — the dispatcher builds a fresh instance per
  request. Set `stateful = True` on the class when you use it.
- **Params may not shadow** `params`, `to_str`, `stateful`, `RESERVED`.

`ctx` carries:

| attr | |
|---|---|
| `phase` | `"request"` or `"response"` |
| `state` | dict scoped per `(tag, name)`, so stateful behaviors isolate per test under one shared proxy across xdist workers |
| `rng` | `random.Random(f"{tag}:{name}")` |
| `tag` | the identity tag |

Params are on `self`, not on `ctx`.

Behavior exceptions are logged and ignored. Unknown names warn and are skipped.

## Status

| stage | |
|---|---|
| **0 — plumbing + CA trust** | done (`tests/test_mitmproxy.py`) |
| **1 — dispatcher end-to-end** | done (`tests/test_proxy_behaviors.py`) — real mitmdump + throwaway origin + a client through the proxy, on loopback, headers set **manually** |
| **2 — profile → delivery** | done. Delivery is preamble SQL via `--init-sqllogic`: `proxy_init_sql()` emits `SET http_proxy` + `SET extra_http_headers`. `{TEST_ID}` is substituted by the binary per test. |
| **2b — suite and matrix routes** | done. `to_init_sql` gives a suite the baseline routing/tag under `auto_init_sql=True`; a matrix cell's `init_sql` property adds behaviors and wins because it is merged last. |
| **3 — driver `.py` route** | done (`tests/test_auto_init_sql.py`). `run_paired(request, init_sql=proxy_init_sql(...))` composes after suite/matrix init SQL and wins. |

## Install

`mitmproxy` ships in the `[mitm]` extra; without it this service is unavailable (its self-tests skip on a
missing `mitmdump`).

`[mitm]` is a superset of `[tls]` — mitm signs with the shared [`ca`](ca.md), which needs `cryptography`.
`[tls]` alone gives the CA and an HTTPS origin without the proxy.
