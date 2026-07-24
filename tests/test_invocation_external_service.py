"""Self-tests for `service(start=None)` -- a service with NO managed lifecycle at all: it just
permanently exists (RESOURCE-PLANNING.md phase 5, `invocation-external`). `provision_service`
routes straight to `attach()` unconditionally, whether or not `--existing-service` was declared --
there's nothing else it could do without a `start`. Offline throughout.
"""

import textwrap

import pytest

from ducktest import Service, service


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# --- pure descriptor shape ----------------------------------------------------------------


def test_start_defaults_to_none():
    svc = service("k")
    assert isinstance(svc, Service)
    assert svc.start is None


def test_start_none_is_explicitly_allowed():
    svc = service("k", start=None, attach=lambda overrides, config: dict(overrides))
    assert svc.start is None


def test_non_callable_start_still_rejected():
    with pytest.raises(TypeError, match="start"):
        service("k", start="not-callable")


# --- provision_service always attaches, no boot, no store entry --------------------------


_CONFTEST = """
    import os
    from ducktest import register_suite, service

    def _attach(overrides, config):
        return {"endpoint": overrides.get("endpoint", "svc://external-default")}

    def _to_env(block):
        return {"SVC_ENDPOINT": block["endpoint"]}

    def _populate(block, config):
        with open(os.environ["POP_LOG"], "a") as f:
            f.write("p\\n")

    def pytest_configure(config):
        register_suite(config, "s", default=True, services=[
            service("svc", start=None, attach=_attach, to_env=_to_env, populate=_populate, fixture="svc_fx")])
"""


def test_no_existing_service_declared_still_attaches_never_boots(pytester, monkeypatch):
    """No `--existing-service` given at all -- a `start=None` service must STILL attach (its only
    possible stance), not error or hang waiting for a boot that can never happen."""
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        import os
        def test_env_from_attach():
            assert os.environ["SVC_ENDPOINT"] == "svc://external-default"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert pop.read_text().count("p") == 1  # populate still runs (idempotent), no boot needed


def test_existing_service_override_still_just_attaches(pytester, monkeypatch):
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        import os
        def test_env_from_attach():
            assert os.environ["SVC_ENDPOINT"] == "svc://overridden"
        """,
    )
    result = pytester.runpytest_subprocess(
        "-n", "0", "--existing-service", "svc=svc://overridden", "-p", "no:cacheprovider"
    )
    result.assert_outcomes(passed=1)


def test_dead_invocation_external_service_fails_loud(pytester):
    """The attach path's `alive` probe still applies: a declared-but-unreachable invocation-external
    service fails loud, same as any other attach (the paradigm-shift stance). Up-front provisioning
    is best-effort (a swallowed failure there is re-surfaced when a fixture pulls it -- see
    `provision_reachable`), so the test pulls the fixture explicitly, mirroring
    test_existing_services.py's `test_existing_service_dead_probe_fails_loud`."""
    _write(
        pytester,
        "conftest.py",
        """
        import pytest
        from ducktest import register_suite, service, get_suites, provision_service

        def _attach(overrides, config):
            return {"endpoint": "svc://nowhere"}

        def _alive(block):
            return False

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("svc", start=None, attach=_attach, alive=_alive, fixture="svc_fx")])

        @pytest.fixture(scope="session")
        def svc_fx(request):
            svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "svc")
            return provision_service(request.config, svc)
        """,
    )
    _write(pytester, "test_inner.py", "def test_a(svc_fx):\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*declared as running*nothing is responding*"])


# --- --provision-service on a start=None service: no crash, no boot attempt --------------


def test_provision_service_cli_reports_invocation_external(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("svc", start=None, attach=lambda overrides, config: {"endpoint": "svc://x"})])
        """,
    )
    result = pytester.runpytest_subprocess("--provision-service", "svc", "-p", "no:cacheprovider")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*svc: invocation-external (no start)*nothing to provision*"])
