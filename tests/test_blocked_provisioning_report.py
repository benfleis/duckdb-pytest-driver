"""A provision that fails is ONE failure with N victims, and the report says so: the owner (the test
whose fixture ran the factory) keeps the real error, every other test is a one-liner naming the
resource, and the summary counts the victims once. Checked under xdist too, where the report is
serialized before `pytest_exception_interact` runs, so a rewrite there would be lost."""

import textwrap

import pytest


def _write(pytester, name, body):
    (pytester.path / name).write_text(textwrap.dedent(body))


_CONFTEST = """
    import pytest
    from ducktest import register_suite, service, provision_service

    def _start(config):
        raise RuntimeError("minio image unpullable: exit 125")

    MINIO = service("minio", start=_start, stop=lambda config: None)

    def pytest_configure(config):
        register_suite(config, "s", default=True, services=[MINIO])

    @pytest.fixture
    def minio(request):
        return provision_service(request.config, MINIO)
"""

_TESTS = """
    def test_a(minio): pass
    def test_b(minio): pass
    def test_c(minio): pass
"""


@pytest.mark.parametrize("workers", ["0", "2"])
def test_blocked_tests_are_one_liners_and_the_cause_is_printed_once(pytester, workers):
    _write(pytester, "conftest.py", _CONFTEST)
    _write(pytester, "test_inner.py", _TESTS)
    result = pytester.runpytest_subprocess("-n", workers, "-p", "no:cacheprovider")
    out = result.stdout.str()
    assert result.ret != 0
    result.assert_outcomes(errors=3)
    assert "provisioning failed: 2 tests blocked" in out
    assert "'minio': 2 tests blocked by this" in out
    assert out.count("BLOCKED: provisioning of 'minio' failed") == 2 + 2  # 2 in the errors, 2 in the short summary
    assert out.count("E       RuntimeError: minio image unpullable") == 1, out  # the owner, once, with its traceback
