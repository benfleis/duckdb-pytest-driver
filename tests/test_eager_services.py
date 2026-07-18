"""Self-tests for up-front service provisioning + env adoption (the service analog of
`credential(adopt="env")`). Offline: FAKE start/populate/to_env logging to files; a bare Python test
reads `os.environ` to prove the derived env was adopted (the stand-in for a bare `.test` subprocess,
which needs a built binary).

Redesign note: the eager/on_demand `provision` disposition is GONE. Collect-first provisions every
service a SELECTED test needs UP FRONT (from the plan) on the controller; workers adopt its env from
the store. `to_env`/`populate` stay (WHAT/HOW env + seed, not WHEN); there is no disposition to choose.
"""

import textwrap

from ducktest import Service, service, use_service


# --- use_service binding (pure) -----------------------------------------------------------


def test_use_service_binds_policy_without_mutating_base():
    # MIGRATED: the `provision` disposition is gone; use_service now binds only to_env/populate policy
    # (and depends_on) onto a shared descriptor without mutating it.
    base = service("azurite", start=lambda c: {"endpoint": "x"})
    assert base.to_env is None and base.populate is None
    bound = use_service(base, to_env=lambda b: {"E": b["endpoint"]}, populate=lambda b, c: None)
    assert isinstance(bound, Service)
    assert bound.to_env is not None and bound.populate is not None
    assert bound.key == "azurite" and bound.start is base.start  # generic bits carried over
    assert base.to_env is None and base.populate is None  # base untouched (shared descriptor)


# REMOVED (was test_service_rejects_bad_provision): the `provision` field no longer exists on the
# Service descriptor (the eager/on_demand disposition is deleted), so there is nothing to reject.


def test_narrating_flag_logic():
    # --steps narrates; --repl/--provision-keep narrate unless --no-steps; --steps wins over --no-steps.
    from ducktest.plugin import _narrating

    class Cfg:
        def __init__(self, **o):
            self._o = o

        def getoption(self, k, default=None):
            return self._o.get(k, default)

    assert _narrating(Cfg()) is False
    assert _narrating(Cfg(**{"--steps": True})) is True
    assert _narrating(Cfg(**{"--repl": True})) is True
    assert _narrating(Cfg(**{"--repl": True, "--no-steps": True})) is False
    assert _narrating(Cfg(**{"--steps": True, "--no-steps": True})) is True


def test_to_env_is_carried_by_the_descriptor():
    # MIGRATED (was test_to_env_allowed_for_any_disposition): to_env is a property of the descriptor,
    # adopted by whatever process provisions the service (the store shares the block). No disposition.
    base = service("x", start=lambda c: None)
    assert use_service(base, to_env=lambda b: {}).to_env is not None
    assert service("x", start=lambda c: None, to_env=lambda b: {}).to_env is not None


# --- eager path end-to-end (isolated pytest run) ------------------------------------------


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_CONFTEST = """
    import os
    from ducktest import register_suite, service

    def _start(config):
        with open(os.environ["BOOT_LOG"], "a") as f: f.write("b\\n")
        return {"endpoint": "svc://up"}

    def _populate(block, config):
        with open(os.environ["POP_LOG"], "a") as f: f.write("p\\n")

    def _to_env(block):
        return {"SVC_ENDPOINT": block["endpoint"]}

    def _attach(overrides, config):
        return {"endpoint": overrides.get("endpoint", "svc://default")}

    def pytest_configure(config):
        register_suite(config, "s", default=True, services=[
            service("svc", start=_start, populate=_populate, to_env=_to_env, attach=_attach)])
"""


def test_up_front_boots_once_adopts_env_populates_once(pytester, monkeypatch):
    boot = pytester.path / "boot.log"
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        import os
        def test_env_adopted():
            # the up-front service's to_env is adopted from the store on the worker (SPEC 3.7)
            assert os.environ["SVC_ENDPOINT"] == "svc://up"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert boot.read_text().count("b") == 1  # booted exactly once (store single-flight)
    assert pop.read_text().count("p") == 1  # populated exactly once


def test_up_front_with_attach_adopts_env_without_booting(pytester, monkeypatch):
    boot = pytester.path / "boot.log"
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        import os
        def test_env_from_attach():
            assert os.environ["SVC_ENDPOINT"] == "svc://external"  # env from the attached endpoint
        """,
    )
    result = pytester.runpytest_subprocess(
        "-n", "0", "--existing-service", "svc=svc://external", "-p", "no:cacheprovider"
    )
    result.assert_outcomes(passed=1)
    assert not boot.exists()  # attach => never booted
    assert pop.read_text().count("p") == 1  # populate still runs (idempotent) against the attached one


def test_populate_runs_exactly_once(pytester, monkeypatch):
    # MIGRATED (was test_on_demand_populates_once): populate has no disposition — a fixture-pulled
    # service still populates exactly once (single-flighted through the store).
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        import pytest
        from ducktest import register_suite, service, get_suites, provision_service

        def _start(config): return {"endpoint": "svc://up"}
        def _populate(block, config):
            with open(os.environ["POP_LOG"], "a") as f: f.write("p\\n")

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("svc", start=_start, populate=_populate, fixture="svc_fx")])

        @pytest.fixture(scope="session")
        def svc_fx(request):
            svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "svc")
            return provision_service(request.config, svc)
        """,
    )
    _write(pytester, "test_inner.py", "def test_a(svc_fx):\n    assert svc_fx['endpoint'] == 'svc://up'\n")
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert pop.read_text().count("p") == 1  # single-flighted: populated exactly once


def test_to_env_adopted_on_fixture_pull(pytester):
    # MIGRATED (was test_on_demand_to_env_adopted_on_fixture_pull): a worker that provisions the service
    # (fixture pull) adopts its to_env into that worker's os.environ. No disposition.
    _write(
        pytester,
        "conftest.py",
        """
        import pytest
        from ducktest import register_suite, service, get_suites, provision_service

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("svc", start=lambda c: {"endpoint": "svc://up"},
                        to_env=lambda b: {"SVC_ENV": b["endpoint"]}, fixture="svc_fx")])

        @pytest.fixture(scope="session")
        def svc_fx(request):
            svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "svc")
            return provision_service(request.config, svc)
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        import os
        def test_env_adopted_worker_side(svc_fx):
            assert os.environ["SVC_ENV"] == "svc://up"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# REMOVED (was test_provision_never_pulled_without_attach_fails_loud): the `provision='never'`
# disposition is deleted. A service with no boot capability is simply expressed as an
# `--existing-service`-only attach target; there is no separate "never" stance to fail loud on.


def test_shared_service_across_suites_boots_and_stops_once(pytester, monkeypatch):
    """A service shared across two suites (the use_service pattern) is ONE physical resource:
    dedup by key means it boots once and — the regression this guards — stops exactly once, not
    once per suite that binds it (a non-idempotent stop() would otherwise double-fire)."""
    boot = pytester.path / "boot.log"
    stop = pytester.path / "stop.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    monkeypatch.setenv("STOP_LOG", str(stop))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        from ducktest import register_suite, service, use_service

        def _start(config):
            with open(os.environ["BOOT_LOG"], "a") as f: f.write("b\\n")
            return {"endpoint": "svc://up"}

        def _stop(config):
            with open(os.environ["STOP_LOG"], "a") as f: f.write("s\\n")

        _BASE = service("svc", start=_start, stop=_stop)

        def pytest_configure(config):
            # two DIFFERENT suites, each binding the SAME shared descriptor eagerly
            register_suite(config, "a", path="a", default=True, services=[use_service(_BASE)])
            register_suite(config, "b", path="b", default=True, services=[use_service(_BASE)])
        """,
    )
    _write(pytester, "test_inner.py", "def test_a():\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert boot.read_text().count("b") == 1  # shared service booted once (store single-flight)
    assert stop.read_text().count("s") == 1  # stopped ONCE, not once per binding suite (the fix)
