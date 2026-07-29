"""mitmproxy addon loaded by ``mitmdump -s``.

This module runs in mitmdump's interpreter and must import with stdlib only. Keep mitmproxy imports
inside behavior methods so pytest can import the same registry and parser without the ``[mitm]`` extra.

Traffic selects behaviors with two headers:

  * ``x-ducktest-proxy-setting``: comma-separated ``name[;k=v;...]`` entries applied in order.
  * ``x-ducktest-test``: per-test tag used for state keys and deterministic RNG seeds.

Behavior params are declared in ``__init__`` and coerced there because wire params are strings.
``__call__(self, flow, ctx)`` implements the request/response effect. The lower_snake_case class name is
the wire name, e.g. ``proxy_init_sql(fail_nth(n=1), latency(ms=500))``.
"""

import asyncio
import inspect
import logging
import random

HEADER_TAG = "x-ducktest-test"
HEADER_SETTING = "x-ducktest-proxy-setting"
HEADER_AUTH = "Proxy-Authorization"  # read by require_auth

# Wire-format separators (see `parse_profile`): behaviors are comma-separated, params `;`-then-`=`.
SEP_BEHAVIOR = ","
SEP_PARAM = ";"
SEP_ASSIGN = "="

_log = logging.getLogger("ducktest.mitm_profile")


# --- the behavior base -----------------------------------------------------------------------------


class ProxyBehavior:
    """Base for a behavior: declare params in ``__init__``, do the work in ``__call__``.

    Subclasses store one attribute per ``__init__`` param with the same name. :meth:`params` reads the
    signature and those attributes to rebuild the wire form.

    ``__init__`` runs in both processes: at pytest import time with Python values and in mitmdump per
    request with strings. It should only coerce params. Per-request state belongs in ``ctx.state``.
    """

    #: Whether the behavior accumulates state across requests, keyed by tag.
    stateful = False

    #: Members a param may not shadow.
    RESERVED = ("params", "to_str", "stateful", "RESERVED")

    def __init_subclass__(cls, **kwargs):
        """Reject a param that shadows a base member, at class-definition time.

        Fires on import of the behavior module — before registration, in either process — so the failure
        names the collision instead of surfacing later as an opaque ``AttributeError`` from ``params()``.
        """
        super().__init_subclass__(**kwargs)
        own = cls.__dict__.get("__init__")
        if own is None:
            return
        for pname in inspect.signature(own).parameters:
            if pname in ProxyBehavior.RESERVED:
                raise TypeError(
                    f"{cls.__name__}.__init__ declares param {pname!r}, which shadows "
                    f"ProxyBehavior.{pname} — pick another name; reserved: {list(ProxyBehavior.RESERVED)}"
                )

    def __init__(self):
        pass

    def __call__(self, flow, ctx):  # pragma: no cover - overridden by every real behavior
        raise NotImplementedError(f"{type(self).__name__} must implement __call__(self, flow, ctx)")

    def params(self) -> dict:
        """The declared params as an all-string dict, read back off ``self``."""
        out = {}
        for pname, p in inspect.signature(type(self).__init__).parameters.items():
            if pname == "self":
                continue
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                raise TypeError(
                    f"{type(self).__name__}.__init__ uses *args/**kwargs; a behavior must declare its "
                    "params by name so they can be rendered onto the wire and validated early."
                )
            if not hasattr(self, pname):
                raise AttributeError(
                    f"{type(self).__name__}.__init__ takes {pname!r} but the instance has no attribute "
                    f"{pname!r} — a behavior stores one attribute per param, under the same name."
                )
            out[pname] = str(getattr(self, pname))
        return out

    def to_str(self) -> str:
        """``latency(ms=500)`` -> ``"latency;ms=500"``.

        Every declared param is emitted, defaults included.
        """
        name = type(self).__name__  # the wire name; `name` stays free as a param
        return SEP_PARAM.join([name] + [f"{k}{SEP_ASSIGN}{v}" for k, v in self.params().items()])

    def __eq__(self, other):
        if not isinstance(other, ProxyBehavior):
            return NotImplemented
        # By name + rendered params, not by class identity: the addon is imported twice (by path in
        # mitmdump, by package on the pytest side), so `type(a) is type(b)` can be false for what is
        # plainly the same behavior.
        return self.to_str() == other.to_str()

    def __hash__(self):
        return hash(self.to_str())

    def __repr__(self):
        return f"<{self.to_str()}>"


# --- behaviors (the built-in registry; extended by `register_proxy_behaviors` on the pytest side) ---


class latency(ProxyBehavior):
    """Delay the response by ``ms`` — a slow-network stand-in. Response phase, so it applies to
    whatever response exists (real or a short-circuited one)."""

    def __init__(self, ms=200):
        self.ms = int(ms)

    async def __call__(self, flow, ctx):
        if ctx.phase == "response":
            await asyncio.sleep(self.ms / 1000)


class status(ProxyBehavior):
    """Short-circuit with an HTTP ``code`` — upstream is never hit."""

    def __init__(self, code=503):
        self.code = int(code)

    def __call__(self, flow, ctx):
        from mitmproxy import http  # deferred: keeps this module stdlib-importable (see module docstring)

        if ctx.phase == "request":
            flow.response = http.Response.make(self.code)


class fail_nth(ProxyBehavior):
    """Fail the first ``n`` requests (per tag) with ``status``, then recover — exercises retry logic.
    Stateful via ``ctx.state`` (keyed per ``(tag, name)``), so concurrent tests don't collide."""

    stateful = True

    def __init__(self, n=1, status=503):
        self.n = int(n)
        self.status = int(status)

    def __call__(self, flow, ctx):
        from mitmproxy import http  # deferred: keeps this module stdlib-importable (see module docstring)

        if ctx.phase == "request":
            ctx.state["n"] = ctx.state.get("n", 0) + 1
            if ctx.state["n"] <= self.n:
                flow.response = http.Response.make(self.status)


class require_auth(ProxyBehavior):
    """Demand proxy auth; return 407 unless the request presents ``Proxy-Authorization``."""

    def __call__(self, flow, ctx):
        from mitmproxy import http  # deferred: keeps this module stdlib-importable (see module docstring)

        if ctx.phase == "request" and HEADER_AUTH not in flow.request.headers:
            flow.response = http.Response.make(
                407, b"proxy auth required", {"Proxy-Authenticate": 'Basic realm="ducktest"'}
            )


BEHAVIORS = {
    "latency": latency,
    "status": status,
    "fail_nth": fail_nth,
    "require_auth": require_auth,
}


# --- dispatcher ------------------------------------------------------------------------------------


class _Ctx:
    __slots__ = ("phase", "rng", "state", "tag")

    def __init__(self, phase, rng, state, tag):
        self.phase = phase
        self.rng = rng
        self.state = state
        self.tag = tag


def parse_profile(value):
    """``"latency;ms=500, fail_nth;n=1"`` -> ``[("latency", {"ms": "500"}), ("fail_nth", {"n": "1"})]``.
    Tolerant: blank entries and malformed ``k=v`` fragments are skipped, not fatal.

    Public because the pytest side parses the same grammar (``Profile.from_str``).
    """
    out = []
    for entry in value.split(SEP_BEHAVIOR):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(SEP_PARAM)
        name = parts[0].strip()
        if not name:
            continue
        params = {}
        for frag in parts[1:]:
            key, sep, val = frag.partition(SEP_ASSIGN)
            if sep:
                params[key.strip()] = val.strip()
        out.append((name, params))
    return out


def _import_behaviors(path):
    """Import a behavior module BY PATH and return its ``BEHAVIORS``. stdlib only (see module docstring).

    Runs in the mitmdump process. The pytest side validated the same file at registration, so anything
    wrong here (missing/empty ``BEHAVIORS``) is a real inconsistency and must be loud, not skipped.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"ducktest_behaviors_{abs(hash(path))}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"behavior module could not be loaded: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = getattr(mod, "BEHAVIORS", None)
    if not isinstance(found, dict) or not found:
        raise RuntimeError(f"behavior module {path} defines no non-empty BEHAVIORS dict")
    return found


class HttpProxy:
    """The ``addons = [...]`` entry: fold the selected behaviors over each flow, in header order."""

    def __init__(self):
        self._state = {}  # (tag, name) -> dict, isolating stateful behaviors per test

    def load(self, loader):
        """Declare the option carrying extra behavior modules (``--set behavior_modules=a.py,b.py``).

        The pytest side registers module PATHS (``register_proxy_behaviors``) and passes them here; the
        classes only ever run in this process. Paths, not objects — the two interpreters share no state.
        """
        loader.add_option(
            "behavior_modules",
            str,
            "",
            "Comma-separated paths to extra behavior modules, each defining BEHAVIORS: name -> class.",
        )

    def configure(self, updated):
        """Merge registered modules' behaviors into :data:`BEHAVIORS` once the option is set."""
        if "behavior_modules" not in updated:
            return
        from mitmproxy import ctx

        for path in (p.strip() for p in ctx.options.behavior_modules.split(SEP_BEHAVIOR)):
            if path:
                BEHAVIORS.update(_import_behaviors(path))

    async def request(self, flow):
        await self._apply("request", flow)

    async def response(self, flow):
        await self._apply("response", flow)

    async def _apply(self, phase, flow):
        spec = flow.request.headers.get(HEADER_SETTING, "")
        if not spec:
            return  # no profile selected on this request -> pure pass-through
        tag = flow.request.headers.get(HEADER_TAG, "")
        for name, params in parse_profile(spec):
            cls = BEHAVIORS.get(name)
            if cls is None:
                # Reached only if something bypassed `Profile.from_str`'s collection-time name check.
                _log.warning("unknown proxy behavior %r (ignored)", name)
                continue
            ctx = _Ctx(phase, random.Random(f"{tag}:{name}"), self._state.setdefault((tag, name), {}), tag)
            try:
                # Constructed per request: `__init__` coerces the all-string wire params, and state
                # deliberately lives in `ctx.state` rather than on the instance, which is disposable.
                res = cls(**params)(flow, ctx)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # noqa: BLE001 - a buggy behavior must not break unrelated traffic
                _log.exception("proxy behavior %r raised (ignored)", name)
            if phase == "request" and flow.response is not None:
                break  # a behavior short-circuited; skip the rest of the request-phase chain


addons = [HttpProxy()]
