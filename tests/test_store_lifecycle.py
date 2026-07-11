"""Self-tests for wiring the shared-state store into the plugin (Step 1: lifecycle).

Offline: no duckdb/op/docker. These spin up an isolated inner pytest run (subprocess, via
pytester) under ``-n 2`` so a genuine xdist WORKER exercises the store the controller started.
The inner assertions live in the inner test bodies — a green inner run == the wiring holds.

Two shapes are proven:
  * a tier that declares a service -> the controller starts the store, publishes its address to
    the env pre-fork, and a worker's ``get_store`` returns a working proxy;
  * NO tier declared -> vanilla: the store env var never appears (nothing started).
"""

import textwrap


def _write(pytester, name, body):
    (pytester.path / name).write_text(textwrap.dedent(body))


def test_worker_sees_store_when_a_tier_declares_a_service(pytester):
    # A tier with a service => the store must start on the controller and reach workers.
    _write(
        pytester,
        "conftest.py",
        """
        def pytest_configure(config):
            from duckdb_pytest_driver import register_tier, service
            register_tier(config, "svc_tier",
                          services=[service("dummy", start=lambda config: {"ok": True})])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        import os

        from duckdb_pytest_driver import get_store, store as S


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
    # No tier declared => nothing new happens: the store env var must be absent everywhere.
    _write(
        pytester,
        "test_inner.py",
        """
        import os

        from duckdb_pytest_driver import get_store


        def test_no_store(request):
            assert "DUCKDB_PYTEST_STORE_ADDR" not in os.environ, "store started for a vanilla run"
            assert get_store(request.config) is None
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
