"""Self-tests for `--repl`'s resource-level init SQL (`_repl_resource_init_sql`) -- the
service/credential analog of a `Provisioner`'s `make_init_sql`, for suites with no provisioner at all
(bare-`.test` suites like azurite/minio). Offline: a live pytest run (real service/credential
provisioning through the store), but no real duckdb binary -- the gather function is called directly
from an inner test body instead of going through `--repl`'s interactive shell launch.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


def test_service_and_credential_contribute_init_sql(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service, credential

        def _start(config):
            return {"endpoint": "svc://up"}

        def _svc_init_sql(block, *, redact=False):
            return f"-- svc secret endpoint={block['endpoint']} redact={redact}\\n"

        def _fetch(config):
            return {"TOKEN": "tok"}

        def _cred_init_sql(value, *, redact=False):
            token = "<redacted>" if redact else value["TOKEN"]
            return f"-- cred secret token={token}\\n"

        def pytest_configure(config):
            register_suite(config, "s", default=True,
                services=[service("svc", start=_start, to_init_sql=_svc_init_sql)],
                credentials=[credential("cred", fetch=_fetch, adopt="env", to_init_sql=_cred_init_sql)])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _repl_resource_init_sql

        def test_gather(request):
            sql = _repl_resource_init_sql(request.config, request.session)
            assert "svc secret endpoint=svc://up redact=False" in sql
            assert "cred secret token=tok" in sql

        def test_gather_redacted(request):
            sql = _repl_resource_init_sql(request.config, request.session, redact=True)
            assert "redact=True" in sql
            assert "cred secret token=<redacted>" in sql
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_service_provision_failure_fails_loud(pytester):
    """Unlike provision_reachable's best-effort service loop, a service needed for --repl init SQL that
    fails to (re-)provision must raise -- not silently give an unexplained bare shell."""
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service

        def _start(config):
            raise RuntimeError("docker not found")

        def _init_sql(block, *, redact=False):
            return "-- unreachable\\n"

        def pytest_configure(config):
            register_suite(config, "s", default=True,
                services=[service("svc", start=_start, to_init_sql=_init_sql)])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        import pytest
        from ducktest.plugin import _repl_resource_init_sql

        def test_gather(request):
            with pytest.raises(pytest.UsageError, match="svc.*docker not found"):
                _repl_resource_init_sql(request.config, request.session)
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_service_without_to_init_sql_contributes_nothing(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service

        def pytest_configure(config):
            register_suite(config, "s", default=True,
                services=[service("svc", start=lambda config: {"endpoint": "svc://up"})])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _repl_resource_init_sql

        def test_gather(request):
            assert _repl_resource_init_sql(request.config, request.session) == ""
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_shared_service_across_suites_contributes_once(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service, use_service

        def _start(config):
            return {"endpoint": "svc://up"}

        def _init_sql(block, *, redact=False):
            return "-- shared secret\\n"

        _BASE = service("svc", start=_start, to_init_sql=_init_sql)

        def pytest_configure(config):
            register_suite(config, "a", path="a", default=True, services=[use_service(_BASE)])
            register_suite(config, "b", path="b", default=True, services=[use_service(_BASE)])
        """,
    )
    _write(pytester, "a/test_a_inner.py", "def test_a():\n    assert True\n")
    _write(
        pytester,
        "b/test_b_inner.py",
        """
        from ducktest.plugin import _repl_resource_init_sql

        def test_gather(request):
            sql = _repl_resource_init_sql(request.config, request.session)
            assert sql.count("shared secret") == 1
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
