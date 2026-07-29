"""Offline end-to-end test of the proxy behavior dispatcher (Stage 1).

Real ``mitmdump`` (with the addon) + a throwaway localhost origin + a client that requests THROUGH the
proxy — all on 127.0.0.1 over plain http, so genuinely offline (no docker, no network, no duckdb). The
behavior/tag headers are set MANUALLY here (delivery renders them from a `Profile` into invocation env —
see ``test_proxy_profile.py`` for the typed half); this isolates "does dispatch work". Proves:
pass-through with no header, ``status`` short-circuit (origin untouched), ``latency`` delay, ``fail_nth``
per-tag state + recovery, a multi-behavior fold, and that a non-fault behavior (``require_auth``) works.
"""

import http.server
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from ducktest.resources import mitmproxy as mitm

pytestmark = pytest.mark.skipif(shutil.which("mitmdump") is None, reason="mitmdump not installed ([mitm] extra)")


class _Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits += 1  # type: ignore[attr-defined]
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(tmp_path):
    """A live origin + a live mitm (with addon) in front of it; yields their URLs + the origin hit count."""
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    origin.hits = 0  # type: ignore[attr-defined]
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    oport = origin.server_address[1]

    block = mitm._spawn(str(tmp_path), _free_port())
    try:
        yield SimpleNamespace(origin=origin, url=f"http://127.0.0.1:{oport}/x", proxy=block["endpoint"])
    finally:
        mitm._shutdown(str(tmp_path))
        origin.shutdown()
        origin.server_close()


def _get(proxy, url, *, tag="t", setting=None, auth=False):
    """GET ``url`` THROUGH ``proxy`` with the ducktest headers; return (status, elapsed_seconds)."""
    headers = {mitm_header_tag(): tag}
    if setting is not None:
        headers[mitm_header_setting()] = setting
    if auth:
        headers["Proxy-Authorization"] = "Basic Zm9vOmJhcg=="
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy}))
    req = urllib.request.Request(url, headers=headers)
    t0 = time.perf_counter()
    try:
        r = opener.open(req, timeout=15)
        return r.status, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter() - t0


def mitm_header_tag():
    from ducktest.resources.mitm_addon import HEADER_TAG

    return HEADER_TAG


def mitm_header_setting():
    from ducktest.resources.mitm_addon import HEADER_SETTING

    return HEADER_SETTING


def test_passthrough_when_no_setting(stack):
    code, _ = _get(stack.proxy, stack.url)  # no behavior header
    assert code == 200
    assert stack.origin.hits == 1  # origin was reached


def test_status_short_circuits(stack):
    code, _ = _get(stack.proxy, stack.url, setting="status;code=503")
    assert code == 503
    assert stack.origin.hits == 0  # upstream never hit


def test_latency_delays(stack):
    _, elapsed = _get(stack.proxy, stack.url, setting="latency;ms=500")
    assert elapsed >= 0.45
    assert stack.origin.hits == 1


def test_fail_nth_is_per_tag_and_recovers(stack):
    c1, _ = _get(stack.proxy, stack.url, tag="A", setting="fail_nth;n=1")
    c2, _ = _get(stack.proxy, stack.url, tag="A", setting="fail_nth;n=1")
    assert c1 == 503  # first fails
    assert c2 == 200  # second recovers (per-tag counter advanced)
    c3, _ = _get(stack.proxy, stack.url, tag="B", setting="fail_nth;n=1")
    assert c3 == 503  # a different tag starts fresh -> isolated state


def test_multi_behavior_fold(stack):
    # both behaviors selected: fail_nth governs the 1st (short-circuit); on the recovered 2nd, latency
    # still applies to the real response. Asserting latency on the 200 sidesteps any question of whether
    # the response hook fires for a synthesized short-circuit response.
    setting = "fail_nth;n=1, latency;ms=300"
    c1, _ = _get(stack.proxy, stack.url, tag="M", setting=setting)
    assert c1 == 503
    c2, e2 = _get(stack.proxy, stack.url, tag="M", setting=setting)
    assert c2 == 200
    assert e2 >= 0.25  # latency composed with fail_nth


_CUSTOM_MODULE = '''
"""An extra behavior module, written the sanctioned way: mitmproxy imported inside __call__."""

from ducktest.resources.mitm_addon import ProxyBehavior


class teapot(ProxyBehavior):
    def __init__(self, code=418):
        self.code = int(code)

    def __call__(self, flow, ctx):
        from mitmproxy import http

        if ctx.phase == "request":
            flow.response = http.Response.make(self.code)


BEHAVIORS = {"teapot": teapot}
'''


def test_registered_behavior_module_reaches_mitmdump(tmp_path):
    """End-to-end for `register_proxy_behaviors`: a custom behavior must actually fire in the proxy.

    Without the `--set behavior_modules=` wiring the pytest side would happily validate `teapot` while
    mitmdump had never heard of it — the addon would log-and-ignore, and the test would pass having
    injected nothing. So this asserts the status the custom behavior produces, not merely that it ran.
    """
    module = tmp_path / "extra_behaviors.py"
    module.write_text(_CUSTOM_MODULE)

    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    origin.hits = 0  # type: ignore[attr-defined]
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{origin.server_address[1]}/x"

    confdir = tmp_path / "conf"
    block = mitm._spawn(str(confdir), _free_port(), behavior_modules=[str(module)])
    try:
        code, _ = _get(block["endpoint"], url, setting="teapot;code=418")
        assert code == 418  # the CUSTOM behavior short-circuited
        assert origin.hits == 0  # upstream never reached
    finally:
        mitm._shutdown(str(confdir))
        origin.shutdown()
        origin.server_close()


def test_require_auth_non_fault_behavior(stack):
    denied, _ = _get(stack.proxy, stack.url, setting="require_auth")
    assert denied == 407  # a non-fault behavior enforces proxy auth
    assert stack.origin.hits == 0
    allowed, _ = _get(stack.proxy, stack.url, setting="require_auth", auth=True)
    assert allowed == 200  # with Proxy-Authorization present, it passes through
