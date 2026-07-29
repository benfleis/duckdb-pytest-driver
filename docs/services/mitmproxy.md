# `mitmproxy` — L7 fault injection + capture

`ducktest.resources.mitmproxy` · key `mitmproxy` · fixture `mitmproxy` · **in-venv subprocess** ·
`[mitm]` extra

A forward proxy duckdb's HTTP traffic routes through, with a Python addon applying **per-request
behaviors** — latency, failures, auth demands — selected by header. Faults are one category of behavior,
not the whole surface.

Runs as a local `mitmdump` subprocess rather than a container because the value here is the addon, and
sharing the venv lets behavior modules import driver/extension code and their deps with no image build,
mount, or dependency skew.

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

The service still depends on [`CA_SERVICE`](ca.md) for boot ordering, because mitmproxy must be seeded
with the root CA before launch. The env is self-contained: proxy routing and CA trust come from
`mitm_env`.

## CA seeding

Before launch, `_spawn` copies the shared CA into the confdir under mitmproxy's required names:

| shared (`.ducktest/ca/`) | seeded (`confdir`) |
|---|---|
| `ducktest-ca.pem` | `mitmproxy-ca.pem` |
| `ducktest-ca-cert.pem` | `mitmproxy-ca-cert.pem` |

Both are copied because, given an existing `mitmproxy-ca.pem`, mitmdump does not re-emit the cert-only
file that readiness waits on. With no CA passed (the config-free test path) mitmdump generates a throwaway
one.

## Lifecycle

Process lifecycle is [`_process.py`](README.md#the-in-venv-subprocess-pattern), shared with
[`httpserver`](httpserver.md). Identity is `Proc("mitm", "mitmproxy")` → `mitm.pid` / `mitm.log` in the
confdir.

This service's own parts:

- **Readiness waits on the listener *and* the CA file** — mitmdump binds before writing the cert a client
  needs to trust it, so a TCP-only probe reports ready too early.
- **`_mitmdump()`** resolves the binary on PATH. `_start` calls it before any CA work, so a base install
  reads "mitmdump not found on PATH" rather than a crypto message about a dependency it also lacks.
- **CA seeding**, above.

The pure `(confdir, port)` core takes no pytest config, so it's testable offline; `_start`/`_stop` are thin
`scratch(...)` wrappers.

| env var | default |
|---|---|
| `DUCKTEST_MITM_PORT` | `18080` |
| `DUCKTEST_MITM_READY_TIMEOUT_S` | `15` |

## The behavior dispatcher (`mitm_addon.py`)

Loaded into mitmdump via `-s`, by file path.

> **Import-clean by contract.** The addon runs inside the mitmdump process — a separate interpreter from
> pytest. It may import only `mitmproxy` + the stdlib, never the driver's pytest-side modules. Every
> behavior module follows the same rule: behaviors are pure `(flow, ctx)` functions the proxy process
> imports, decoupled from the framework that *selects* them.

### Selection headers

duckdb carries these via the `http_extra_headers` setting, written by `proxy_init_sql` into the
`--init-sqllogic` preamble.

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
| `require_auth` | — | 407 unless `Proxy-Authorization` is present |

`require_auth` isn't a fault; it's there to keep the surface general.

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

The signature **is** the schema. There is no param table to maintain: an unknown keyword is Python's own
`TypeError` at the call site, and each default lives in exactly one place. Class names are
lower_snake_case because the class name is the wire name and the call site reads as a declaration.

Conventions, each enforced:

- **One attribute per param, same name** — `to_str()` reads the signature, then reads those attributes
  back. A missing one is an `AttributeError` naming the convention.
- **`__init__` coerces and nothing else.** It runs in both processes: on the pytest side at decoration
  time with Python values, in mitmdump per request with wire strings. No I/O, no state.
- **State goes in `ctx.state`**, not on the instance — the dispatcher builds a fresh instance per
  request. Set `stateful = True` on the class when you use it.
- **Params may not shadow** `params`, `to_str`, `stateful`, `RESERVED`; `__init_subclass__` rejects that
  at class-definition time. The reserved set is kept tiny — the wire name is read off the class rather
  than stored, precisely so `name` stays usable as a param.

`ctx` carries:

| attr | |
|---|---|
| `phase` | `"request"` or `"response"` |
| `state` | dict scoped per `(tag, name)`, so stateful behaviors isolate per test under one shared proxy across xdist workers |
| `rng` | `random.Random(f"{tag}:{name}")` |
| `tag` | the identity tag |

Params are on `self`, not on `ctx`.

A behavior that raises is logged and ignored, so a buggy one can't break unrelated traffic. An unknown
name warns and is skipped. Blank entries and malformed `k=v` fragments are dropped, not fatal.

## Status

| stage | |
|---|---|
| **0 — plumbing + CA trust** | done (`tests/test_mitmproxy.py`) |
| **1 — dispatcher end-to-end** | done (`tests/test_proxy_behaviors.py`) — real mitmdump + throwaway origin + a client through the proxy, on loopback, headers set **manually** |
| **2 — profile → delivery** | done. Delivery is **preamble SQL** via `--init-sqllogic`: `mitm_profile.proxy_init_sql()` emits `SET http_proxy` + `SET http_extra_headers`. One builder, because `http_extra_headers` is a single JSON setting — two `SET`s clobber rather than merge, so the identity tag and the behaviors must be emitted together. The tag is `{TEST_ID}`, substituted by the binary **per test**, so one preamble shared by a batched invocation still keys proxy state per test. Secrets are out (only the best-matching secret is selected, so a service's S3 secret and these headers cannot coexist), and there is deliberately no pytest marker. |
| **2b — the two routes** | the service's `to_init_sql` gives a suite the baseline (routing + tag) under `auto_init_sql=True`; a matrix cell's `init_sql` property adds behaviors and, merged last, wins. No cell-property seam in core — `init_sql` already is one. |
| **3 — driver `.py` route** | **not built.** `run_paired` never passes `--init-sqllogic`, so a driver test cannot select a profile. See `next.md`. |

## Install

`mitmproxy` ships in the `[mitm]` extra, marked `python_version >= '3.12'`. The marker keeps the driver's
3.9 floor (it vendors into duckdb core) while satisfying uv's universal lock: modern mitmproxy needs 3.12+,
so on older interpreters the dep drops out and this service is unavailable (its self-tests skip on a
missing `mitmdump`).

`[mitm]` is a superset of `[tls]` — mitm signs with the shared [`ca`](ca.md), which needs `cryptography`.
`[tls]` alone has no python floor, so the CA and an HTTPS origin still work where the proxy can't install.
