"""Pytest-side proxy profile declaration and validation.

``mitm_addon`` owns the behaviors and wire parser and runs inside mitmdump. This module owns typed
profile construction, early validation, and the preamble SQL sent to duckdb.

A profile reaches the binary as preamble SQL (:func:`proxy_init_sql`), run before each body via
``--init-sqllogic``:

- the mitm service's ``to_init_sql`` — the suite-wide baseline (proxy routing + the identity tag),
  picked up by any suite with ``auto_init_sql=True``.
- a matrix cell's ``init_sql`` property, which adds behaviors and runs last::

      {"id": "flaky", "properties": {"init_sql": proxy_init_sql(fail_nth(n=1))}}

The identity tag must differ per test. The binary substitutes ``{TEST_ID}`` per test case, so a single
preamble shared by a batched invocation still keys proxy state per test.

Driver ``.py`` tests have no route yet — ``run_paired`` doesn't pass ``--init-sqllogic``.

A behavior is a class you instantiate. Python resolves the name and keywords at import:
``latencu(...)`` is a NameError, ``latency(msec=500)`` is a TypeError.

The string form stays supported for a shared named profile: ``Profile.from_str("fail_nth;n=1")``.
"""

import importlib.util
import json
import os
import sys
from dataclasses import dataclass

from ..sqldef import sql_literal
from .mitm_addon import (
    BEHAVIORS,
    HEADER_SETTING,
    HEADER_TAG,
    SEP_ASSIGN,
    SEP_BEHAVIOR,
    SEP_PARAM,
    ProxyBehavior,
    parse_profile,
)

# httpfs extension options (`httpfs_extension.cpp`). `http_extra_headers` is a JSON object merged into
# every request's headers; a secret's own EXTRA_HTTP_HEADERS still wins per key. Both need httpfs
# loaded, hence the `require` line the preamble opens with.
HEADERS_SETTING = "http_extra_headers"
PROXY_SETTING = "http_proxy"

# Substituted by the unittest binary per test (`SQLLogicTestRunner::ReplaceKeywords`, from
# `environment_variables["TEST_ID"]`), so one preamble shared by every file in a batched invocation
# still keys proxy state per test. Left intact by the driver — the binary fills it in.
TAG_PLACEHOLDER = "{TEST_ID}"

# Module-level because validation runs while conftests and test modules are imported.
_REGISTERED_PATHS: list = []  # ordered — mitmdump is handed the same set, in the same order
_REGISTERED_BEHAVIORS: dict = {}  # name -> class, merged into the validation set

# These separate the wire format, so a name/key/value containing one would re-parse as a different
# profile on the far side.
_FORBIDDEN = (SEP_BEHAVIOR, SEP_PARAM, SEP_ASSIGN)


def known_behaviors() -> dict:
    """The addon's built-ins plus any registered module's."""
    return {**BEHAVIORS, **_REGISTERED_BEHAVIORS}


def behavior_name(spec) -> str:
    """The wire name for ``spec``, read off the CLASS so ``name`` stays usable as a behavior param."""
    if isinstance(spec, ProxyBehavior):
        return type(spec).__name__
    if isinstance(spec, type):
        return spec.__name__
    if isinstance(spec, str):
        return spec
    raise TypeError(f"proxy profile: cannot take a behavior name from {type(spec).__name__}")


def _accepted(cls) -> list:
    """The param names ``cls.__init__`` declares — for error messages only."""
    import inspect

    return [p for p in inspect.signature(cls.__init__).parameters if p != "self"]


def _check_token(kind, value) -> str:
    text = str(value)
    if not text.strip():
        raise ValueError(f"proxy profile: {kind} must be a non-empty string, got {value!r}")
    bad = [c for c in _FORBIDDEN if c in text]
    if bad:
        raise ValueError(
            f"proxy profile: {kind} {text!r} may not contain {bad!r} — those separate the wire format "
            f"({SEP_BEHAVIOR!r} between behaviors, {SEP_PARAM!r} before a param, {SEP_ASSIGN!r} in it)."
        )
    return text.strip()


def _construct(cls, name, params):
    """Build one behavior from wire params (all strings). Where a misspelled param dies."""
    try:
        return cls(**params)
    except TypeError as e:
        raise ValueError(
            f"proxy profile: behavior {name!r} rejected params {sorted(params)} ({e}); it accepts "
            f"{sorted(_accepted(cls))}. An unknown key would be ignored by the proxy, applying the "
            "default and passing."
        ) from e


@dataclass(frozen=True)
class Profile:
    """An ordered set of behaviors.

    Order is preserved and significant: the addon folds behaviors in this order, and a request-phase
    behavior that short-circuits (sets ``flow.response``) stops the ones after it.
    """

    behaviors: tuple = ()

    def __post_init__(self):
        items = tuple(self.behaviors)
        for b in items:
            if not isinstance(b, ProxyBehavior):
                raise TypeError(
                    f"Profile: behaviors must be ProxyBehavior instances, got {type(b).__name__} "
                    "— instantiate the behavior class, e.g. latency(ms=500)"
                )
        object.__setattr__(self, "behaviors", items)

    def to_str(self) -> str:
        """The ``x-ducktest-proxy-setting`` header value."""
        return ", ".join(b.to_str() for b in self.behaviors)

    @classmethod
    def from_str(cls, text, *, validate=True) -> "Profile":
        """Parse ``"latency;ms=500, fail_nth;n=1"`` into behavior instances via the addon's parser.

        ``validate=False`` skips only the wire-safety token checks, which :func:`as_profile` then runs
        over the whole set; names are always resolved.
        """
        parsed = parse_profile(text)
        known = known_behaviors()
        unknown = sorted({n for n, _ in parsed if n not in known})
        if unknown:
            raise ValueError(_unknown_message(unknown, known))
        built = tuple(_construct(known[n], n, p) for n, p in parsed)
        if validate:
            _validate(built)
        return cls(behaviors=built)

    def to_headers(self, tag) -> dict:
        """The two headers a request must carry for this profile to apply.

        ``tag`` keys stateful behaviors and seeds their RNG, so concurrent tests under one shared proxy
        don't share ``fail_nth`` counters.
        """
        return {HEADER_SETTING: self.to_str(), HEADER_TAG: str(tag)}


def _unknown_message(unknown, known) -> str:
    return (
        f"proxy profile: unknown behavior(s) {unknown!r}; known: {sorted(known)}. "
        "Register a module of extra behaviors with register_proxy_behaviors(path)."
    )


def _validate(behaviors) -> None:
    """Reject unknown names and wire-corrupting names/params.

    Param keys need no check — they came from an ``__init__`` signature, so an unknown one already
    failed in :func:`_construct`.
    """
    known = known_behaviors()
    unknown = sorted({behavior_name(b) for b in behaviors if behavior_name(b) not in known})
    if unknown:
        raise ValueError(_unknown_message(unknown, known))
    for b in behaviors:
        _check_token("behavior name", behavior_name(b))
        for key, value in b.params().items():
            _check_token("param key", key)
            _check_token("param value", value)


def as_profile(*specs) -> Profile:
    """Coerce behavior specs into one validated :class:`Profile`.

    Each spec may be a behavior instance, a behavior class (no params), a wire string, a
    :class:`Profile`, or an iterable of those. Multiple specs concatenate in order.
    """
    if len(specs) == 1 and isinstance(specs[0], Profile):
        _validate(specs[0].behaviors)  # re-validate against the CURRENT registry
        return specs[0]
    built = []
    for spec in specs:
        built.extend(_coerce(spec))
    profile = Profile(behaviors=tuple(built))
    _validate(profile.behaviors)
    return profile


def _coerce(spec) -> list:
    """One spec -> behavior instances. Unvalidated; :func:`as_profile` validates the set."""
    if isinstance(spec, ProxyBehavior):
        return [spec]
    if isinstance(spec, Profile):
        return list(spec.behaviors)
    if isinstance(spec, str):
        return list(Profile.from_str(spec, validate=False).behaviors)
    if isinstance(spec, type):
        return [_construct(spec, spec.__name__, {})]  # a bare class = its declared defaults
    try:
        items = list(spec)
    except TypeError:
        raise TypeError(
            "proxy profile: expected a behavior instance, a behavior class, a wire string, a Profile, "
            f"or an iterable of those; got {type(spec).__name__}"
        ) from None
    out = []
    for item in items:
        if isinstance(item, str):
            # Ambiguous with a wire string, so `[fail_nth]` and `["fail_nth"]` can't mean subtly
            # different things.
            raise TypeError(
                f"proxy profile: {item!r} inside an iterable must be a behavior instance or class "
                "— pass a wire string on its own, or instantiate the behavior"
            )
        out.extend(_coerce(item))
    return out


# --- delivery: profile -> preamble SQL --------------------------------------------------------------


def proxy_init_sql(*specs, endpoint=None) -> str:
    """Preamble SQL selecting ``specs``' behaviors for the body that runs after it.

    The one builder for both routes, because ``http_extra_headers`` is a single JSON-object setting:
    two ``SET`` statements would clobber rather than merge, so tag and profile must be emitted
    together.

    - The mitm service's ``to_init_sql`` calls it with ``endpoint=`` for the suite-wide baseline
      (routing + tag, no behaviors).
    - A matrix cell calls it with behaviors, as its ``init_sql`` property::

          {"id": "flaky", "properties": {"init_sql": proxy_init_sql(fail_nth(n=1))}}

      Cell SQL is merged after the suite's (``plugin._init_sqllogic_arg_for_item``), so the cell's
      ``SET`` runs last and wins — the routing set by the baseline persists on the same connection.

    Behaviors are validated here, i.e. at conftest import, before anything boots.
    """
    headers = as_profile(*specs).to_headers(TAG_PLACEHOLDER)
    lines = ["require httpfs", ""]  # the settings below are httpfs's, so it must be loaded first
    if endpoint:
        lines.append(f"SET {PROXY_SETTING} = {sql_literal(endpoint)};")
    payload = json.dumps({k: v for k, v in headers.items() if v})  # drop the empty setting header
    lines.append(f"SET {HEADERS_SETTING} = {sql_literal(payload)};")
    return "\n".join(lines) + "\n"


# --- extra behavior modules ------------------------------------------------------------------------


def register_proxy_behaviors(path) -> str:
    """Register a module of extra behavior classes; returns its absolute path. Idempotent per path.

    Imported here to read its ``BEHAVIORS`` so names validate on the pytest side; the path is recorded
    so the mitm service can hand the same file to mitmdump, where the behaviors actually run.

    The module must import with the stdlib alone — import ``mitmproxy`` inside ``__call__``, as
    ``mitm_addon`` does. Checked statically (:func:`_reject_module_scope_mitmproxy`) so it fails the
    same way with or without the ``[mitm]`` extra installed; relying on the import to blow up would
    never catch it on the author's machine.
    """
    abspath = os.path.abspath(str(path))
    if not os.path.isfile(abspath):
        raise ValueError(f"register_proxy_behaviors: no such module file: {abspath}")
    if abspath in _REGISTERED_PATHS:
        return abspath

    _reject_module_scope_mitmproxy(abspath)
    mod = _load_behavior_module(abspath)
    found = getattr(mod, "BEHAVIORS", None)
    if not isinstance(found, dict) or not found:
        raise ValueError(
            f"register_proxy_behaviors: {abspath} must define a non-empty BEHAVIORS dict mapping name -> class."
        )
    for name, cls in found.items():
        _check_token("behavior name", name)
        if not (isinstance(cls, type) and issubclass(cls, ProxyBehavior)):
            raise TypeError(
                f"register_proxy_behaviors: BEHAVIORS[{name!r}] in {abspath} is not a ProxyBehavior "
                "subclass — subclass ducktest.resources.mitm_addon.ProxyBehavior, declare params in "
                "__init__, and do the work in __call__(self, flow, ctx)."
            )
        if name in BEHAVIORS:
            raise ValueError(
                f"register_proxy_behaviors: {abspath} redefines built-in behavior {name!r}; "
                "shadowing a built-in would make the same profile string mean different things."
            )
    _REGISTERED_PATHS.append(abspath)
    _REGISTERED_BEHAVIORS.update(found)
    return abspath


def registered_behavior_modules() -> tuple:
    """The registered module paths, in registration order — what the mitm service passes to mitmdump."""
    return tuple(_REGISTERED_PATHS)


def _reject_module_scope_mitmproxy(abspath) -> None:
    """Fail if the module imports ``mitmproxy`` at module scope.

    Static (``ast``) and top-level only, so the result doesn't depend on what happens to be installed
    here, and says nothing about imports inside methods — which are the sanctioned form.
    """
    import ast

    with open(abspath, encoding="utf-8") as f:
        source = f.read()
    try:
        tree = ast.parse(source, filename=abspath)
    except SyntaxError as e:
        raise ValueError(f"register_proxy_behaviors: {abspath} is not valid Python: {e}") from e

    for node in tree.body:
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        if any(n == "mitmproxy" or n.startswith("mitmproxy.") for n in names):
            raise ImportError(
                f"register_proxy_behaviors: {abspath} imports mitmproxy at module scope "
                f"(line {node.lineno}). Import it inside __call__ instead — the module is loaded by "
                "mitmdump (to run the behaviors) and by pytest (to validate profile names), and the "
                "pytest side may not have the [mitm] extra installed."
            )


def _load_behavior_module(abspath):
    name = "ducktest_proxy_behaviors_" + str(abs(hash(abspath)))
    spec = importlib.util.spec_from_file_location(name, abspath)
    if spec is None or spec.loader is None:
        raise ValueError(f"register_proxy_behaviors: cannot load a module from {abspath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:
        # The mitmproxy case is already rejected statically, so this is some other missing dependency.
        del sys.modules[name]
        raise ImportError(
            f"register_proxy_behaviors: {abspath} could not be imported ({e}). A behavior module is "
            "loaded by both mitmdump and pytest, so it must import cleanly in a plain interpreter — "
            "keep third-party imports inside the behavior methods."
        ) from e
    except Exception:
        del sys.modules[name]
        raise
    return mod
