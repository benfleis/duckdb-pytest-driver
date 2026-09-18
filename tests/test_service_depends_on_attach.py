"""Repro: session teardown must survive a managed service that depends_on an ATTACHED one.

A managed `rest` declares `depends_on=("minio",)`; `minio` is supplied via `--existing-service`, so it
is ATTACHED -- never booted, never written to the store. `_stop_services` builds its "started" set
from store presence, so the attached `minio` is absent from it, and `_topological_service_order`
raises `UsageError` on `rest`'s now-unsatisfiable dep -- aborting `sessionfinish` teardown (and leaking
the store manager). This is the container split-brain case the design exists to serve: attaching a
dependency must not perturb the owned lifecycle.

Currently FAILS (the teardown sort raises); passes once teardown ignores deps outside the started set.
"""

import textwrap

import pytest


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_CONFTEST = """
    import pytest
    from ducktest import register_suite, service, provision_service

    def _rest_start(config):
        return {"endpoint": "svc://rest"}

    def _minio_start(config):
        return {"endpoint": "svc://minio-booted"}  # never called: minio is attached

    def _noop_stop(config):
        pass

    def _minio_attach(overrides, config):
        return {"endpoint": overrides.get("endpoint", "svc://minio")}

    MINIO = service("minio", start=_minio_start, stop=_noop_stop, attach=_minio_attach)
    REST = service("rest", start=_rest_start, stop=_noop_stop, depends_on=("minio",))

    def pytest_configure(config):
        register_suite(config, "s", default=True, services=[REST, MINIO])

    @pytest.fixture(scope="session")
    def rest_fx(request):
        return provision_service(request.config, REST)
"""


@pytest.mark.xfail(strict=True, reason="teardown sorts over deps outside the started set; fix pending")
def test_teardown_survives_managed_dependent_of_attached_dependency(pytester):
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        def test_uses_rest(rest_fx):
            assert rest_fx["endpoint"] == "svc://rest"
        """,
    )
    result = pytester.runpytest_subprocess(
        "-n", "0", "--existing-service", "minio=svc://external", "-p", "no:cacheprovider"
    )
    # The defect is at session teardown, AFTER the inner test runs green -- so assert the teardown
    # signal first (a crash eats the terminal summary, which would make assert_outcomes throw its own
    # unrelated error). These two fail now and pass once teardown ignores deps outside the started set.
    joined = "\n".join(result.outlines + result.errlines)
    assert "no service with that key is registered" not in joined, (
        "teardown's topological sort raised on the attached (unstored) dependency"
    )
    assert result.ret == 0, "session teardown crashed on a managed dependent of an attached service"
    result.assert_outcomes(passed=1)
