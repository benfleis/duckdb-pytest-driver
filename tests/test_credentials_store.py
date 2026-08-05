"""Self-tests for collect-first credential delivery through the store.

Offline: the credential ``fetch`` is a FAKE (never real op) that appends its pid to a log so the outer
test can count fetches. One env-driven conftest serves every scenario:
  FETCH_LOG   append pid on fetch
  CRED_VALID  "0" -> validate() fails
  DBX_PRESENT "1" -> available() true (creds "already in env")
Isolated inner pytest runs (subprocess, via pytester) exercise the real controller/worker split.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_CRED_CONFTEST = """
    import os

    from ducktest import register_suite, credential


    def _fetch(config):
        with open(os.environ["FETCH_LOG"], "a") as f:
            f.write(str(os.getpid()) + "\\n")
        return {"DBX_TOKEN": "TOK"}

    def _validate(value):
        return os.environ.get("CRED_VALID", "1") == "1" and bool(value)

    def _available():
        return os.environ.get("DBX_PRESENT") == "1"

    def _error():
        return "DBX creds unavailable: run your env script"

    def pytest_configure(config):
        register_suite(config, "dbx", path="test/dbx", default=True,
            credentials=[credential("dbx_creds", fetch=_fetch, validate=_validate,
                         error=_error, adopt="env", available=_available)])
"""


def test_credential_fetched_once_up_front_and_reaches_worker(pytester, monkeypatch):
    """`test/dbx/*` items genuinely belong to the `dbx` suite (path membership) -- unlike a root-level
    test, which would no longer see this credential at all now that adoption is suite-scoped, not
    global (a root-level test happening to see it on a bare `default=True` run was exactly the leak
    this scoping removes)."""
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(
        pytester,
        "test/dbx/test_inner.py",
        """
        from ducktest import get_store, store as S
        from ducktest.plugin import _item_env


        def _check(request):
            assert _item_env(request.config, request.node)["DBX_TOKEN"] == "TOK"  # scoped to the suite
            st = get_store(request.config)
            assert S.copy(st, "dbx_creds") == {"DBX_TOKEN": "TOK"}  # block readable on the worker

        def test_a(request): _check(request)
        def test_b(request): _check(request)
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert log.read_text().count("\n") == 1  # up-front fetch, controller only


def test_unreachable_suite_not_fetched(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test_inner.py", "def test_a(): pass")
    # -m other: nothing matches -> no in-suite item runs -> no fetch.
    pytester.runpytest_subprocess("-m", "other", "-p", "no:cacheprovider")
    assert not log.exists()


def test_invalid_credential_errors_the_session(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "0")  # up-front validate -> False
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test_inner.py", "def test_a(): pass")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0  # session aborted (UsageError, pre-fork)
    result.stderr.fnmatch_lines(["*DBX creds unavailable*"])


def test_available_env_short_circuits_fetch_under_k(pytester, monkeypatch):
    # Collect-first resolves -k; available() avoids a fetch when the environment is ready.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("DBX_PRESENT", "1")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not log.exists()  # available() short-circuited before any fetch


def test_collect_first_fetches_credential_under_k(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert log.read_text().count("\n") == 1


def test_invalid_credential_under_k_aborts_the_session(pytester, monkeypatch):
    # Invalid credentials abort during collect-first provisioning, before tests execute.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "0")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    assert result.ret != 0  # session aborted up front (UsageError), not a per-test error
    assert log.exists()  # the up-front fetch WAS attempted (then rejected by validate)
    result.stderr.fnmatch_lines(["*DBX creds unavailable*"])


def test_vanilla_credential_free_run_unaffected(pytester, monkeypatch):
    # A test outside the suite's path does not make the suite reachable.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/oss/test_local.py", "def test_ok(): pass")
    result = pytester.runpytest_subprocess("test/oss", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not log.exists()  # 'dbx' path doesn't intersect test/oss -> no fetch
