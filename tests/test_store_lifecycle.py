"""Self-tests for wiring the shared-state store into the plugin (Step 1: lifecycle).

Offline: no duckdb/op/docker. These spin up an isolated inner pytest run (subprocess, via
pytester) under ``-n 2`` so a genuine xdist WORKER exercises the store the controller started.
The inner assertions live in the inner test bodies — a green inner run == the wiring holds.

Two shapes are proven:
  * a suite that declares a service -> the controller starts the store, publishes its address to
    the env pre-fork, and a worker's ``get_store`` returns a working proxy;
  * NO suite declared -> vanilla: the store env var never appears (nothing started).
"""

import textwrap


def _write(pytester, name, body):
    (pytester.path / name).write_text(textwrap.dedent(body))


def test_store_teardown_restores_preexisting_environment(monkeypatch, tmp_path):
    import os

    from ducktest import service
    from ducktest import store as store_module
    from ducktest.context import SessionContext, set_context
    from ducktest.plugin import _ensure_store_started, _teardown_store
    from ducktest.suites import Suite

    class _Proxy:
        def get(self, key):
            return None

    class _Manager:
        def __init__(self):
            self.proxy = _Proxy()
            self.shutdown_called = False

        def store(self):
            return self.proxy

        def shutdown(self):
            self.shutdown_called = True

    class _Config:
        def __init__(self):
            self.stash = {}

    manager = _Manager()
    config = _Config()
    config._duckdb_suites = [Suite("s", services=(service("svc", start=lambda c: {}),))]
    set_context(config, SessionContext(working_dir=str(tmp_path), run_id="test-run"))
    monkeypatch.setattr(store_module, "start_server", lambda: (manager, "inner-address", b"inner-key"))
    monkeypatch.setenv(store_module.ADDR_ENV, "outer-address")
    monkeypatch.setenv(store_module.KEY_ENV, "outer-key")

    _ensure_store_started(config)
    assert store_module.from_env()[0] == "inner-address"
    _teardown_store(config)

    assert manager.shutdown_called
    assert os.environ.get(store_module.ADDR_ENV) == "outer-address"
    assert os.environ.get(store_module.KEY_ENV) == "outer-key"


def test_worker_sees_store_when_a_suite_declares_a_service(pytester):
    # A suite with a service => the store must start on the controller and reach workers.
    _write(
        pytester,
        "conftest.py",
        """
        def pytest_configure(config):
            from ducktest import register_suite, service
            register_suite(config, "svc_suite",
                          services=[service("dummy", start=lambda config: {"ok": True})])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        import os

        from ducktest import get_store, store as S


        def _check(request):
            # runs on an xdist worker under -n 2: the address was inherited from the controller
            assert os.environ.get("DUCKDB_PYTEST_STORE_ADDR"), "worker did not inherit store addr"
            st = get_store(request.config)
            assert st is not None, "get_store returned None on a worker with a store"
            S.put(st, "probe", {"v": 1})          # prove it's a live proxy: round-trip a block
            assert S.copy(st, "probe") == {"v": 1}

        def test_a(request): _check(request)
        def test_b(request): _check(request)
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_vanilla_starts_no_store(pytester):
    # No suite declared => nothing new happens: the store env var must be absent everywhere.
    _write(
        pytester,
        "test_inner.py",
        """
        import os

        from ducktest import get_store


        def test_no_store(request):
            assert "DUCKDB_PYTEST_STORE_ADDR" not in os.environ, "store started for a vanilla run"
            assert get_store(request.config) is None
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
