"""Offline tests for the typed proxy-profile half (`ducktest.resources.mitm_profile`) — no mitmdump, no network.

Covers the properties the design rests on:

1. ``mitm_addon`` imports with stdlib only, so the pytest side can read ``BEHAVIORS`` and reuse
   ``parse_profile`` without the ``[mitm]`` extra. Checked in a subprocess with ``mitmproxy`` masked, so
   the assertion is about the module and not about what happens to be installed here.
2. ``to_str``/``from_str`` round-trip through the addon's parser.
3. An unknown behavior name raises at declaration time. The addon can only log-and-ignore one
   (``_apply``), which would run a test with no behavior applied and pass — a false green.
4. A misspelled param is the same false green one level down, and dies on the ``__init__`` signature —
   the class-based API's reason for existing.
"""

import importlib
import re
import subprocess
import sys
import textwrap

import pytest

from ducktest.resources import mitm_addon
from ducktest.resources.mitm_addon import HEADER_SETTING, HEADER_TAG, ProxyBehavior, fail_nth, latency, require_auth
from ducktest.resources.mitm_profile import (
    TAG_PLACEHOLDER,
    Profile,
    as_profile,
    behavior_name,
    known_behaviors,
    proxy_init_sql,
    register_proxy_behaviors,
    registered_behavior_modules,
)

ADDON_PATH = mitm_addon.__file__


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot/restore the module-level behavior registry so registration tests don't leak.

    """
    pp = importlib.import_module("ducktest.resources.mitm_profile")

    paths = list(pp._REGISTERED_PATHS)
    behaviors = dict(pp._REGISTERED_BEHAVIORS)
    yield
    pp._REGISTERED_PATHS[:] = paths
    pp._REGISTERED_BEHAVIORS.clear()
    pp._REGISTERED_BEHAVIORS.update(behaviors)


# --- 1. the addon is importable without mitmproxy -------------------------------------------------


def test_addon_imports_with_stdlib_alone():
    """The load-bearing property: `from mitmproxy import http` is deferred into the behaviors that need
    it, so validation on the pytest side needs no [mitm] extra. Run with mitmproxy masked."""
    probe = textwrap.dedent(
        """
        import importlib.util, sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "mitmproxy" or name.startswith("mitmproxy."):
                    raise ImportError("mitmproxy blocked")
                return None
        sys.meta_path.insert(0, Block())
        spec = importlib.util.spec_from_file_location("addon_probe", sys.argv[1])
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        print(",".join(sorted(m.BEHAVIORS)))
        """
    )
    out = subprocess.run([sys.executable, "-c", probe, ADDON_PATH], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, f"addon needs mitmproxy at import time:\n{out.stderr}"
    assert out.stdout.strip() == "fail_nth,latency,require_auth,status"


# --- 2. one grammar, round-tripped ----------------------------------------------------------------


def test_behavior_round_trip():
    b = latency(ms=500)
    assert b.to_str() == "latency;ms=500"
    assert b.params() == {"ms": "500"}  # all-string, so the wire round-trip is identity
    assert Profile.from_str(b.to_str()).behaviors[0] == b


def test_to_str_emits_declared_defaults():
    """The header states what the proxy will apply, rather than leaving it to a default to look up."""
    assert latency().to_str() == "latency;ms=200"
    assert fail_nth().to_str() == "fail_nth;n=1;status=503"
    assert require_auth().to_str() == "require_auth"  # no params declared


def test_profile_round_trip_preserves_order():
    p = Profile.from_str("fail_nth;n=1;status=503, latency;ms=500")
    assert [behavior_name(b) for b in p.behaviors] == ["fail_nth", "latency"]  # order is significant
    assert Profile.from_str(p.to_str()) == p


def test_from_str_uses_the_addons_parser():
    """Anything the proxy accepts, this accepts — the drift guard."""
    wire = "latency;ms=500 , fail_nth;n=2"
    assert [n for n, _ in mitm_addon.parse_profile(wire)] == [
        behavior_name(b) for b in Profile.from_str(wire).behaviors
    ]


def test_from_str_coerces_through_init():
    """Wire params are strings; `__init__` is the one place that coerces them."""
    b = Profile.from_str("latency;ms=500").behaviors[0]
    assert b.ms == 500 and isinstance(b.ms, int)


def test_to_headers_emits_the_two_addon_headers():
    headers = Profile.from_str("latency;ms=10").to_headers("nodeid::x")
    assert headers == {HEADER_SETTING: "latency;ms=10", HEADER_TAG: "nodeid::x"}


# --- 3. unknown names are loud --------------------------------------------------------------------


def test_unknown_behavior_name_raises():
    with pytest.raises(ValueError, match="unknown behavior"):
        Profile.from_str("latencyy;ms=500")


def test_as_profile_validates_where_the_profile_is_built():
    with pytest.raises(ValueError, match="unknown behavior"):
        as_profile("no_such_behavior")


def test_an_unregistered_behavior_class_is_rejected():
    class not_registered(ProxyBehavior):
        def __call__(self, flow, ctx):
            pass

    with pytest.raises(ValueError, match="unknown behavior"):
        as_profile(not_registered())


# --- 4. a misspelled param dies on the signature --------------------------------------------------


def test_misspelled_param_is_a_typeerror_at_the_call_site():
    """`latency(msec=500)` would silently apply the 200ms default and pass — Python rejects it first."""
    with pytest.raises(TypeError):
        latency(msec=500)


def test_misspelled_param_in_the_string_form_names_what_is_accepted():
    with pytest.raises(ValueError, match=r"rejected params.*accepts.*'ms'"):
        Profile.from_str("latency;msec=500")


def test_declared_params_are_accepted():
    assert as_profile(fail_nth(n=1, status=503)).to_str() == "fail_nth;n=1;status=503"


# --- require_auth credentials ----------------------------------------------------------------------


def test_require_auth_with_credentials_round_trips():
    b = require_auth(username="alice", password="s3cret")
    assert b.to_str() == "require_auth;username=alice;password=s3cret"
    assert Profile.from_str(b.to_str()).behaviors[0] == b


def test_require_auth_bare_still_omits_credentials_from_the_wire():
    # Unset creds must not appear as the literal string "None" on the wire.
    assert require_auth().to_str() == "require_auth"


def test_require_auth_rejects_one_sided_credentials():
    with pytest.raises(ValueError, match="username and password must be set together"):
        require_auth(username="alice")
    with pytest.raises(ValueError, match="username and password must be set together"):
        require_auth(password="s3cret")


# --- wire-format safety ---------------------------------------------------------------------------


_STRING_PARAM_MODULE = '''
"""A behavior with a free-form STRING param — the only way a separator can reach the wire."""

from ducktest.resources.mitm_addon import ProxyBehavior


class set_header(ProxyBehavior):
    def __init__(self, name="X-Test", value="1"):
        self.name = str(name)
        self.value = str(value)

    def __call__(self, flow, ctx):
        from mitmproxy import http  # noqa: F401

        if ctx.phase == "request":
            flow.request.headers[self.name] = self.value


BEHAVIORS = {"set_header": set_header}
'''


@pytest.fixture
def set_header(tmp_path):
    register_proxy_behaviors(_write(tmp_path, "string_param.py", _STRING_PARAM_MODULE))
    return known_behaviors()["set_header"]


@pytest.mark.parametrize("bad", ["a,b", "a;b", "a=b"])
def test_separator_in_param_value_raises(set_header, bad):
    """A value containing a separator would be re-parsed as a different profile — reject it."""
    with pytest.raises(ValueError, match="may not contain"):
        as_profile(set_header(value=bad))


def test_separator_in_a_registered_name_raises(tmp_path):
    """A class name cannot contain one, but the BEHAVIORS key is free-form — so it is checked."""
    body = _STRING_PARAM_MODULE.replace('"set_header": set_header', '"set;header": set_header')
    with pytest.raises(ValueError, match="may not contain"):
        register_proxy_behaviors(_write(tmp_path, "badname.py", body))


# --- coercion + the marker ------------------------------------------------------------------------


def test_as_profile_accepts_every_spec_shape():
    want = Profile((latency(ms=5),))
    assert as_profile("latency;ms=5") == want
    assert as_profile(latency(ms=5)) == want
    assert as_profile(want) == want
    assert as_profile([latency(ms=5)]) == want


def test_as_profile_concatenates_varargs_in_order():
    p = as_profile(fail_nth(n=1), latency(ms=5))
    assert [behavior_name(b) for b in p.behaviors] == ["fail_nth", "latency"]
    assert p.to_str() == "fail_nth;n=1;status=503, latency;ms=5"


def test_a_bare_class_is_that_behavior_with_its_defaults():
    assert as_profile(require_auth).to_str() == "require_auth"
    assert as_profile(latency).to_str() == "latency;ms=200"


def test_a_profile_extends_rather_than_replaces_when_named():
    """How a test adds to a suite default instead of overriding it."""
    default = as_profile(latency(ms=50))
    assert as_profile(default, fail_nth(n=2)).to_str() == "latency;ms=50, fail_nth;n=2;status=503"


def test_as_profile_rejects_a_non_behavior():
    with pytest.raises(TypeError):
        as_profile(42)
    with pytest.raises(TypeError, match="must be a behavior instance or class"):
        as_profile(["latency"])  # ambiguous with a wire string -> rejected inside an iterable


def test_behavior_name_resolves_a_class_and_an_instance():
    assert behavior_name(fail_nth) == "fail_nth"
    assert behavior_name(fail_nth(n=2)) == "fail_nth"
    assert behavior_name("fail_nth") == "fail_nth"


def test_statefulness_is_declared():
    """Declared, not inferred — a shared tag (a batch of bare bodies) can then be rejected."""
    assert fail_nth.stateful is True
    assert latency.stateful is False


def _headers(sql):
    """The header dict out of a rendered `SET extra_http_headers = map_concat(..., MAP {...});`."""
    body = re.search(r"MAP \{(.*)\}\);", sql).group(1)
    pairs = re.findall(r"'((?:[^']|'')*)':\s*'((?:[^']|'')*)'", body)
    return {k.replace("''", "'"): v.replace("''", "'") for k, v in pairs}


def test_baseline_preamble_routes_and_tags():
    sql = proxy_init_sql(endpoint="http://127.0.0.1:18080")
    assert sql.startswith("require httpfs\n\n")  # the settings are httpfs's
    assert "SET http_proxy = 'http://127.0.0.1:18080';" in sql
    assert "map_concat(current_setting('extra_http_headers')" in sql  # composed, not replaced
    # No behaviors selected, so no setting header at all rather than an empty one.
    assert _headers(sql) == {mitm_addon.HEADER_TAG: TAG_PLACEHOLDER}


def test_cell_preamble_carries_tag_and_profile_in_ONE_statement():
    sql = proxy_init_sql(fail_nth(n=1), latency(ms=500))
    assert sql.count("SET extra_http_headers") == 1
    assert _headers(sql) == {
        mitm_addon.HEADER_SETTING: "fail_nth;n=1;status=503, latency;ms=500",
        mitm_addon.HEADER_TAG: TAG_PLACEHOLDER,
    }


def test_cell_preamble_sets_no_route():
    # Routing comes from the service baseline and persists on the same connection; a cell that also
    # emitted it would just restate it.
    assert "SET http_proxy" not in proxy_init_sql(latency(ms=1))


def test_tag_is_left_for_the_binary_to_substitute_per_test():
    # `{TEST_ID}` survives verbatim: SQLLogicTestRunner::ReplaceKeywords fills it per test case, so one
    # preamble shared by a batched invocation still keys proxy state per test.
    assert TAG_PLACEHOLDER in proxy_init_sql(latency(ms=1))
    assert TAG_PLACEHOLDER == "{TEST_ID}"


def test_behaviors_are_validated_when_the_preamble_is_built():
    with pytest.raises(ValueError, match="unknown behavior"):
        proxy_init_sql("no_such_behavior")


def test_a_quote_in_a_param_value_cannot_break_out_of_the_sql_literal(set_header):
    sql = proxy_init_sql(set_header(value="it's"))
    assert "''" in sql
    assert _headers(sql)[mitm_addon.HEADER_SETTING] == "set_header;name=X-Test;value=it's"


# --- registering extra behavior modules -----------------------------------------------------------


_GOOD_MODULE = '''
"""A well-behaved extra behavior module: mitmproxy imported INSIDE __call__."""

from ducktest.resources.mitm_addon import ProxyBehavior


class drop(ProxyBehavior):
    def __init__(self, code=499):
        self.code = int(code)

    def __call__(self, flow, ctx):
        from mitmproxy import http

        if ctx.phase == "request":
            flow.response = http.Response.make(self.code)


BEHAVIORS = {"drop": drop}
'''

_BAD_MODULE = """
from mitmproxy import http

from ducktest.resources.mitm_addon import ProxyBehavior


class drop(ProxyBehavior):
    def __call__(self, flow, ctx):
        flow.response = http.Response.make(499)


BEHAVIORS = {"drop": drop}
"""


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def test_registering_a_module_makes_its_names_usable(tmp_path):
    path = _write(tmp_path, "extra.py", _GOOD_MODULE)
    assert "drop" not in known_behaviors()
    assert register_proxy_behaviors(path) == path
    assert "drop" in known_behaviors()
    assert behavior_name(Profile.from_str("drop").behaviors[0]) == "drop"  # now validates
    assert path in registered_behavior_modules()  # and reaches mitmdump


def test_a_registered_behaviors_params_are_checked_by_its_own_signature(tmp_path):
    """Custom behaviors get exactly the check the built-ins get — there is no schema to opt into."""
    register_proxy_behaviors(_write(tmp_path, "extra.py", _GOOD_MODULE))
    assert as_profile(Profile.from_str("drop;code=418")).to_str() == "drop;code=418"
    with pytest.raises(ValueError, match=r"rejected params.*accepts.*'code'"):
        Profile.from_str("drop;cod=418")


def test_registration_is_idempotent(tmp_path):
    path = _write(tmp_path, "extra.py", _GOOD_MODULE)
    register_proxy_behaviors(path)
    register_proxy_behaviors(path)
    assert registered_behavior_modules().count(path) == 1


def test_module_scope_mitmproxy_import_is_rejected_with_the_rule(tmp_path):
    """Checked statically, so it fails identically whether or not [mitm] is installed here — otherwise
    the author (who has the extra) sees success and someone else's CI sees the failure."""
    path = _write(tmp_path, "bad.py", _BAD_MODULE)
    with pytest.raises(ImportError, match="inside __call__"):
        register_proxy_behaviors(path)
    assert path not in registered_behavior_modules()


@pytest.mark.parametrize("stmt", ["import mitmproxy", "from mitmproxy import http", "import mitmproxy.http"])
def test_every_module_scope_mitmproxy_import_form_is_caught(tmp_path, stmt):
    body = f"{stmt}\n\n{_GOOD_MODULE}"
    with pytest.raises(ImportError, match="module scope"):
        register_proxy_behaviors(_write(tmp_path, "bad.py", body))


def test_nested_mitmproxy_import_is_allowed(tmp_path):
    """The sanctioned form — the whole point of the rule is that this passes."""
    path = _write(tmp_path, "ok.py", _GOOD_MODULE)
    assert register_proxy_behaviors(path) == path


def test_syntax_error_is_reported_as_such(tmp_path):
    path = _write(tmp_path, "broken.py", "def f(:\n")
    with pytest.raises(ValueError, match="not valid Python"):
        register_proxy_behaviors(path)


def test_shadowing_a_builtin_is_rejected(tmp_path):
    body = _GOOD_MODULE.replace('"drop": drop', '"latency": drop')
    with pytest.raises(ValueError, match="redefines built-in"):
        register_proxy_behaviors(_write(tmp_path, "shadow.py", body))


def test_a_non_proxybehavior_entry_is_rejected(tmp_path):
    """A plain function or class has no declared params, so it cannot be rendered onto the wire."""
    body = "def drop(flow, ctx):\n    pass\n\n\nBEHAVIORS = {'drop': drop}\n"
    with pytest.raises(TypeError, match="not a ProxyBehavior subclass"):
        register_proxy_behaviors(_write(tmp_path, "plain.py", body))


def test_module_without_behaviors_is_rejected(tmp_path):
    path = _write(tmp_path, "empty.py", "X = 1\n")
    with pytest.raises(ValueError, match="non-empty BEHAVIORS"):
        register_proxy_behaviors(path)


def test_missing_module_file_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="no such module file"):
        register_proxy_behaviors(str(tmp_path / "nope.py"))
