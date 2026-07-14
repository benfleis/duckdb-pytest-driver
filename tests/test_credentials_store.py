"""Self-tests for eager + late credential delivery via the store (Step 2 + late-fetch).

Offline: the credential ``fetch`` is a FAKE (never real op) that appends its pid to a log so the outer
test can count fetches. One env-driven conftest serves every scenario:
  FETCH_LOG   append pid on fetch
  CRED_VALID  "0" -> validate() fails
  DBX_PRESENT "1" -> available() true (creds "already in env")
  LATE_FETCH  "0" -> credential(late_fetch=False)
Isolated inner pytest runs (subprocess, via pytester) exercise the real controller/worker split.

Backstop resolution order (per credential): store (up-front) -> available() env -> late fetch -> fail.
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
                         error=_error, adopt="env", available=_available,
                         late_fetch=os.environ.get("LATE_FETCH", "1") == "1")])
"""


def test_credential_fetched_once_up_front_and_reaches_worker(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        import os

        from ducktest import get_store, store as S


        def _check(request):
            assert os.environ.get("DBX_TOKEN") == "TOK"        # adopt="env" reached the worker
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
    # -m other: nothing matches -> no in-suite item runs -> no fetch (up-front OR backstop).
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


def test_backstop_available_env_short_circuits_late_fetch(pytester, monkeypatch):
    # -k (unpredicted) with creds already in env -> available() passes, no fetch/op.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("DBX_PRESENT", "1")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not log.exists()  # available() short-circuited before any fetch


def test_backstop_late_fetch_rescues_under_k(pytester, monkeypatch):
    # -k, not in env -> late fetch (default on) provisions it -> the test passes; fetch ran once.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert log.read_text().count("\n") == 1  # single-flight late fetch happened


def test_backstop_poisons_on_invalid_late_fetch(pytester, monkeypatch):
    # late fetch runs but validate fails -> ProvisionFailed poison -> the test errors loud.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "0")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    assert log.exists()  # the late fetch WAS attempted (then poisoned)


def test_backstop_fails_fast_when_late_fetch_disabled(pytester, monkeypatch):
    # late_fetch=False + not in env + unreachable -> fail loud, never fetch (strict CI).
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("LATE_FETCH", "0")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/dbx/test_live.py", "def test_live(): pass")
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    assert not log.exists()  # no fetch attempted


def test_vanilla_credential_free_run_unaffected(pytester, monkeypatch):
    # A test outside the suite's path -> not in suite -> no backstop, no fetch.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/oss/test_local.py", "def test_ok(): pass")
    result = pytester.runpytest_subprocess("test/oss", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not log.exists()  # 'dbx' path doesn't intersect test/oss -> no fetch
