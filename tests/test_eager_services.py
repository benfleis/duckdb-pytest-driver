"""Self-tests for eager service provisioning + env adoption (Gap 1 — the service analog of
`credential(adopt="env")`). Offline: FAKE start/populate/to_env logging to files; a bare Python test
reads `os.environ` to prove the derived env was adopted pre-fork (the stand-in for a bare `.test`
subprocess, which needs a built binary).
"""

import textwrap

from ducktest import Service, service, use_service


# --- use_service binding (pure) -----------------------------------------------------------


def test_use_service_binds_policy_without_mutating_base():
    base = service("azurite", start=lambda c: {"endpoint": "x"})
    assert base.provision == "on_demand" and base.to_env is None
    bound = use_service(base, provision="eager", to_env=lambda b: {"E": b["endpoint"]}, populate=lambda b, c: None)
    assert isinstance(bound, Service)
    assert bound.provision == "eager" and bound.to_env is not None and bound.populate is not None
    assert bound.key == "azurite" and bound.start is base.start  # generic bits carried over
    assert base.provision == "on_demand" and base.to_env is None  # base untouched (shared descriptor)


def test_service_rejects_bad_provision():
    import pytest

    with pytest.raises(ValueError, match="provision"):
        service("x", start=lambda c: None, provision="whenever")


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
            service("svc", start=_start, populate=_populate, to_env=_to_env, attach=_attach,
                    provision="eager")])
"""


def test_eager_boots_once_adopts_env_populates_once(pytester, monkeypatch):
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
            # the eager service's to_env landed in os.environ pre-fork -> the worker inherits it
            assert os.environ["SVC_ENDPOINT"] == "svc://up"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert boot.read_text().count("b") == 1  # controller booted once, pre-fork
    assert pop.read_text().count("p") == 1  # populated exactly once


def test_eager_with_attach_adopts_env_without_booting(pytester, monkeypatch):
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


def test_provision_never_pulled_without_attach_fails_loud(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        import pytest
        from ducktest import register_suite, service, get_suites, provision_service

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("svc", start=lambda c: {"x": 1}, fixture="svc_fx", provision="never")])

        @pytest.fixture(scope="session")
        def svc_fx(request):
            svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "svc")
            return provision_service(request.config, svc)
        """,
    )
    _write(pytester, "test_inner.py", "def test_a(svc_fx):\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)  # pulled with no --existing-service to attach to
    result.stdout.fnmatch_lines(["*provision='never'*attach*"])
