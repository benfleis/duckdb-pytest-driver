"""Self-tests for `Service.depends_on` resolution -- topological start order + reverse-order
teardown (RESOURCE-PLANNING.md phase 6; subsumes the Iceberg `rest`->`minio` case in
`docs/PLAN.md` § *Multi-service dependencies*). Offline: FAKE start/stop that append an ordered
line to a shared log, so the outer test can read back the actual sequence.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_ORDER_CONFTEST = """
    import os
    import pytest
    from ducktest import register_suite, service, provision_service

    def _mk_start(name):
        def _start(config):
            with open(os.environ["ORDER_LOG"], "a") as f:
                f.write(f"start:{name}\\n")
            return {"endpoint": f"svc://{name}"}
        return _start

    def _mk_stop(name):
        def _stop(config):
            with open(os.environ["ORDER_LOG"], "a") as f:
                f.write(f"stop:{name}\\n")
        return _stop

    MINIO = service("minio", start=_mk_start("minio"), stop=_mk_stop("minio"))
    # declared BEFORE its dependency, on purpose -- start order must come from depends_on, not
    # declaration order.
    REST = service("rest", start=_mk_start("rest"), stop=_mk_stop("rest"), depends_on=("minio",))

    def pytest_configure(config):
        register_suite(config, "s", default=True, services=[REST, MINIO])

    @pytest.fixture(scope="session")
    def rest_fx(request):
        return provision_service(request.config, REST)
"""


def test_dependency_boots_before_dependent_and_stops_after(pytester, monkeypatch):
    log = pytester.path / "order.log"
    monkeypatch.setenv("ORDER_LOG", str(log))
    _write(pytester, "conftest.py", _ORDER_CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        def test_uses_rest(rest_fx):
            assert rest_fx["endpoint"] == "svc://rest"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    lines = log.read_text().splitlines()
    assert lines.index("start:minio") < lines.index("start:rest")  # dependency boots first
    assert lines.index("stop:rest") < lines.index("stop:minio")  # dependent stops first (reverse)


def test_dependency_started_even_when_only_the_dependent_is_pulled(pytester, monkeypatch):
    """Pulling only `rest_fx` (never `minio` directly) still boots `minio` -- start order falls out
    of the recursive resolution, no separate fixture/pull needed for the dependency."""
    log = pytester.path / "order.log"
    monkeypatch.setenv("ORDER_LOG", str(log))
    _write(pytester, "conftest.py", _ORDER_CONFTEST)
    _write(pytester, "test_inner.py", "def test_a(rest_fx):\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert "start:minio" in log.read_text()


def test_shared_dependency_boots_once_across_two_dependents(pytester, monkeypatch):
    """Two dependents sharing the same dependency (two suites' services BOTH depending on `minio`)
    still boot the dependency exactly once (single-flighted, same as any other shared service)."""
    log = pytester.path / "order.log"
    monkeypatch.setenv("ORDER_LOG", str(log))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        import pytest
        from ducktest import register_suite, service, provision_service

        def _mk_start(name):
            def _start(config):
                with open(os.environ["ORDER_LOG"], "a") as f:
                    f.write(f"start:{name}\\n")
                return {"endpoint": f"svc://{name}"}
            return _start

        MINIO = service("minio", start=_mk_start("minio"))
        A = service("a", start=_mk_start("a"), depends_on=("minio",))
        B = service("b", start=_mk_start("b"), depends_on=("minio",))

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[MINIO, A, B])

        @pytest.fixture(scope="session")
        def a_fx(request):
            return provision_service(request.config, A)

        @pytest.fixture(scope="session")
        def b_fx(request):
            return provision_service(request.config, B)
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        def test_a(a_fx):
            assert True

        def test_b(b_fx):
            assert True
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert log.read_text().count("start:minio") == 1


def test_undeclared_dependency_fails_loud(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        import pytest
        from ducktest import register_suite, service, provision_service

        BAD = service("bad", start=lambda c: {}, depends_on=("nonexistent",))

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[BAD])

        @pytest.fixture(scope="session")
        def bad_fx(request):
            return provision_service(request.config, BAD)
        """,
    )
    _write(pytester, "test_inner.py", "def test_a(bad_fx):\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*bad*depends_on*nonexistent*no service*registered*"])


def test_dependency_cycle_fails_loud(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        import pytest
        from ducktest import register_suite, service, provision_service

        A = service("a", start=lambda c: {}, depends_on=("b",))
        B = service("b", start=lambda c: {}, depends_on=("a",))

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[A, B])

        @pytest.fixture(scope="session")
        def a_fx(request):
            return provision_service(request.config, A)
        """,
    )
    _write(pytester, "test_inner.py", "def test_a(a_fx):\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*cycle*"])
