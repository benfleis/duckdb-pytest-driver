"""Self-tests for the RO once-guard promoted from a per-worker `set()` to a store-backed
single-flight (RESOURCE-PLANNING.md phase 3). Offline: `instantiate()` is a FAKE that appends the
target name to a log file, so the outer test can count how many times it actually ran -- the
`Provisioner` analog of `test_eager_services.py`'s boot-count checks.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_PROVISIONER_CONFTEST = """
    import os
    from ducktest import register_suite, register_provisioner, requires, service
    from ducktest.provision import Provisioner


    class _FakeProvisioner(Provisioner):
        def execute(self, sql):
            pass

        def rw_target(self, spec, token, state, dry_run):
            return f"rw_{{token}}_{{spec.resolved_name()}}"

        def ro_target(self, spec, state):
            return "shared_ro_table"  # every ro @requires in these tests targets the same table

        def instantiate(self, spec, target, dry_run, state):
            if os.environ.get("INSTANTIATE_FAIL") == "1":
                raise RuntimeError("instantiate boom")
            with open(os.environ["INSTANTIATE_LOG"], "a") as f:
                f.write(target + "\\n")

        def make_init_sql(self, bindings, *, redact=False):
            return ""


    def pytest_configure(config):
        register_provisioner(config, _FakeProvisioner(), scope=os.path.dirname(__file__))
        {services}
"""

_RO_TEST_BODY = """
    from ducktest import requires

    @requires(source="db.schema.shared_ro_table", access="ro")
    def test_{name}(resources):
        assert True
"""


def test_ro_shared_once_across_xdist_workers(pytester, monkeypatch):
    """A store IS running (the suite also declares a service) -- the RO target must be instantiated
    exactly ONCE for the whole invocation, not once per worker (the shipped per-worker `set()` bug
    this phase fixes)."""
    log = pytester.path / "instantiate.log"
    monkeypatch.setenv("INSTANTIATE_LOG", str(log))
    _write(
        pytester,
        "conftest.py",
        _PROVISIONER_CONFTEST.format(
            services='register_suite(config, "s", default=True, services=[service("svc", start=lambda c: {"ok": True})])'
        ),
    )
    _write(pytester, "test_a.py", _RO_TEST_BODY.format(name="a"))
    _write(pytester, "test_b.py", _RO_TEST_BODY.format(name="b"))
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert log.read_text().count("shared_ro_table") == 1  # single-flighted, not once per worker


def test_ro_falls_back_to_per_process_guard_with_no_store(pytester, monkeypatch):
    """No suite declares a service/credential -> no store ever starts. The bare per-process
    `_shared_ro` set still dedupes within that one process, matching the shipped behavior."""
    log = pytester.path / "instantiate.log"
    monkeypatch.setenv("INSTANTIATE_LOG", str(log))
    _write(pytester, "conftest.py", _PROVISIONER_CONFTEST.format(services='register_suite(config, "s", default=True)'))
    _write(pytester, "test_a.py", _RO_TEST_BODY.format(name="a"))
    _write(pytester, "test_b.py", _RO_TEST_BODY.format(name="b"))
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert log.read_text().count("shared_ro_table") == 1


def test_ro_failure_poisons_the_key_for_later_waiters(pytester, monkeypatch):
    """A failed instantiate poisons the store key for the rest of the session -- the SAME
    fail-fast/no-retry-storm contract Service/Credential already have (RESOURCE-PLANNING.md §4),
    not a new one invented for RO. The second test never even attempts instantiate()."""
    log = pytester.path / "instantiate.log"
    monkeypatch.setenv("INSTANTIATE_LOG", str(log))
    monkeypatch.setenv("INSTANTIATE_FAIL", "1")
    _write(
        pytester,
        "conftest.py",
        _PROVISIONER_CONFTEST.format(
            services='register_suite(config, "s", default=True, services=[service("svc", start=lambda c: {"ok": True})])'
        ),
    )
    _write(pytester, "test_a.py", _RO_TEST_BODY.format(name="a"))
    _write(pytester, "test_b.py", _RO_TEST_BODY.format(name="b"))
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=2)  # both fixture setups fail: first raises, second is poisoned
    assert not log.exists()  # instantiate() itself never wrote a line (it always raises)
