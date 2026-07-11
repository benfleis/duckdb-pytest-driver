"""Self-tests for lazy first-need service provisioning via the store (Step 3).

Offline: the service's ``start``/``stop`` are FAKE (no docker) and append their pid to log files so
the outer test can count invocations. An isolated inner pytest run under ``-n 2`` proves the real
controller/worker split: the first worker to need the service provisions it (single-flight), and the
controller tears it down once at session end.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


def test_service_started_once_and_stopped_once(pytester, monkeypatch):
    start_log = pytester.path / "start.log"
    stop_log = pytester.path / "stop.log"
    monkeypatch.setenv("START_LOG", str(start_log))
    monkeypatch.setenv("STOP_LOG", str(stop_log))
    _write(
        pytester,
        "conftest.py",
        """
        import os

        import pytest

        from duckdb_pytest_driver import register_tier, service, get_tiers, provision_service


        def _start(config):
            with open(os.environ["START_LOG"], "a") as f:
                f.write(str(os.getpid()) + "\\n")
            return {"url": "svc://local"}

        def _stop(config):
            with open(os.environ["STOP_LOG"], "a") as f:
                f.write(str(os.getpid()) + "\\n")

        def pytest_configure(config):
            register_tier(config, "svc_tier", default=True,
                services=[service("dummy", start=_start, stop=_stop, fixture="dummy_service")])

        @pytest.fixture(scope="session")
        def dummy_service(request):
            svc = next(s for t in get_tiers(request.config) for s in t.services if s.key == "dummy")
            return provision_service(request.config, svc)
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        def test_a(dummy_service):
            assert dummy_service["url"] == "svc://local"

        def test_b(dummy_service):
            assert dummy_service["started"] is True
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert start_log.read_text().count("\n") == 1  # single-flight: started exactly once
    assert stop_log.read_text().count("\n") == 1  # controller stopped it exactly once


def test_service_provisions_under_k_selection(pytester, monkeypatch):
    """A service is demand-driven: pulling its fixture provisions it even under -k, which args can't
    predict. (Regression: an earlier reachability gate hard-failed the -k case.)"""
    start_log = pytester.path / "start.log"
    monkeypatch.setenv("START_LOG", str(start_log))
    _write(
        pytester,
        "conftest.py",
        """
        import os

        import pytest

        from duckdb_pytest_driver import register_tier, service, get_tiers, provision_service


        def _start(config):
            with open(os.environ["START_LOG"], "a") as f:
                f.write(str(os.getpid()) + "\\n")
            return {"url": "svc://local"}

        def pytest_configure(config):
            register_tier(config, "svc_tier", default=True,
                services=[service("dummy", start=_start, fixture="dummy_service")])

        @pytest.fixture(scope="session")
        def dummy_service(request):
            svc = next(s for t in get_tiers(request.config) for s in t.services if s.key == "dummy")
            return provision_service(request.config, svc)
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        def test_wanted(dummy_service):
            assert dummy_service["url"] == "svc://local"

        def test_other(dummy_service):
            assert True
        """,
    )
    # -k selects only test_wanted; args can't predict the tier, but the fixture request provisions it.
    # (xdist doesn't surface `deselected` in the controller aggregate, so assert only passed + the log.)
    result = pytester.runpytest_subprocess("-n", "2", "-k", "wanted", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert start_log.read_text().count("\n") == 1  # provisioned once, no reachability hard-fail
