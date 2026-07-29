"""mitmproxy addon: the proxy-profile behavior dispatcher (loaded by ``mitmdump -s``).

IMPORT-CLEAN by contract: this runs INSIDE the mitmdump process (a separate interpreter from pytest),
loaded by file PATH — so it may import only ``mitmproxy`` + the stdlib, never the driver's pytest-side
modules (no ``from ..suites import …``). That's the same discipline every behavior module must follow:
behaviors are pure ``(flow, ctx)`` callables the proxy process imports, decoupled from the framework
that *selects* them.

The ``mitmproxy`` import is DEFERRED into the behaviors that need it so this module also imports
cleanly with the stdlib ALONE. That is what lets the pytest side (``mitm_profile``) read
:data:`BEHAVIORS` and reuse :func:`parse_profile` for validation without the ``[mitm]`` extra installed
— one registry and one grammar shared by both processes, instead of a second copy that drifts. Every
registered behavior module must follow the same rule (import ``mitmproxy`` inside the method, not at
module scope) or it becomes unreadable from the pytest side.

Selection is per-request, by two headers duckdb carries (verified end-to-end against a real build):

  * ``x-ducktest-proxy-setting`` — a COMMA-LIST of behaviors to apply, each ``name[;k=v;…]``
    (e.g. ``latency;ms=500, fail_nth;n=1``). Applied in order; a behavior that short-circuits (sets
    ``flow.response`` in the request phase) stops later request-phase behaviors.
  * ``x-ducktest-test`` — the per-test IDENTITY tag. Keys per-``(tag, name)`` state (so stateful
    behaviors isolate per test under one shared proxy across xdist workers) and seeds a reproducible
    per-``(tag, name)`` RNG. Later also the capture-attribution key.

A behavior is a CLASS: params are declared in ``__init__`` (which coerces, because wire params arrive as
strings) and the effect goes in ``__call__(self, flow, ctx)``, sync or async. Declaring params in a
signature is what makes a typo — ``latency(msec=500)`` — a plain ``TypeError`` at the call site rather
than a silently-applied default. ``ctx`` carries ``phase`` (``"request"``/``"response"``), ``rng``,
``state`` (a per-``(tag, name)`` dict) and ``tag``; params are on ``self``.

Behavior classes are named in lower_snake_case, unlike normal Python classes: the class name IS the wire
name, and the call site reads as a declaration (``proxy_init_sql(fail_nth(n=1), latency(ms=500))``).

A *fault* is just one CATEGORY of behavior here — ``require_auth`` sits beside ``latency``/``fail_nth``
to prove the surface is general, which is why the vocabulary is "behavior" (one callable) and "profile"
(an ordered set of them), not "fault".
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

    Subclasses follow one convention — **one attribute per ``__init__`` param, same name** — because
    :meth:`params` reads the signature and then reads those attributes back to rebuild the wire form.
    That is what removes the separate param table the function-based version needed: the signature IS
    the schema, so an unknown key is Python's own ``TypeError`` and a default lives in exactly one
    place.

    ``__init__`` runs in BOTH processes — on the pytest side at decoration time with Python values, and
    in mitmdump per request with strings off the wire. So it must coerce (``int(ms)``) and must do
    nothing else: no I/O, no state. Per-request state belongs in ``ctx.state``, which is keyed
    ``(tag, name)`` and survives across the fresh instance the dispatcher builds for each request.
    """

    #: Whether the behavior accumulates state across requests (keyed by tag). Declared, not inferred,
    #: so a shared tag — a batch of bare `.test` bodies under one invocation — can be rejected rather
    #: than silently sharing one counter across every body in the batch (see findings.md).
    stateful = False

    #: Members a param may not shadow. Deliberately TINY — every name here is one a behavior author
    #: cannot use, and `name`/`value`/`code` are exactly the words a header or status behavior wants.
    #: That is why the wire name is read off the CLASS rather than kept as an instance attribute.
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

        EVERY declared param is emitted, defaults included, so the header states exactly what the proxy
        will apply instead of leaving it to a default the reader has to look up.
        """
        name = type(self).__name__  # the wire name; NOT an attribute, so `name` stays free as a param
        return SEP_PARAM.join([name] + [f"{k}{SEP_ASSIGN}{v}" for k, v in self.params().items()])

    def __eq__(self, other):
        if not isinstance(other, ProxyBehavior):
            return NotImplemented
        # By name + rendered params, NOT by class identity: the addon is imported twice (by path in
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
    """A NON-fault behavior (proves the surface is general): demand proxy auth, 407 unless the request
    presents ``Proxy-Authorization``."""

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

    PUBLIC because the pytest side parses the same grammar (``Profile.from_str``). Sharing this one
    function is what makes the two sides structurally incapable of disagreeing about the format.
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
                # Reached only if something bypassed `Profile.from_str`'s name check (which errors at
                # collection): an inert behavior here would be a silent false green.
                _log.warning("unknown proxy behavior %r (ignored)", name)
                continue
            ctx = _Ctx(phase, random.Random(f"{tag}:{name}"), self._state.setdefault((tag, name), {}), tag)
            try:
                # Constructed per request: `__init__` coerces the all-string wire params, and state
                # deliberately lives in `ctx.state` rather than on the instance, which is disposable.
                res = cls(**params)(flow, ctx)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # a buggy behavior must not break unrelated traffic
                _log.exception("proxy behavior %r raised (ignored)", name)
            if phase == "request" and flow.response is not None:
                break  # a behavior short-circuited; skip the rest of the request-phase chain


addons = [HttpProxy()]
