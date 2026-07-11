"""Self-tests for eager credential delivery via the store (Step 2).

Offline: the credential ``fetch`` is a FAKE (never real op) that appends its pid to a log file so
the outer test can assert it ran exactly once, on the controller. Isolated inner pytest runs
(subprocess, via pytester) exercise the real controller/worker split.

Proven:
  * a reachable credentialed tier on a bare run -> ``fetch`` runs once (controller only), the block
    is readable via ``store.copy`` on a worker, and an ``adopt="env"`` value reaches the worker env;
  * a run selecting a different tier (``-m other``) -> ``fetch`` is NOT called (unreachable);
  * an invalid credential (``validate`` False) -> the session errors with the ``error()`` message;
  * the runtest backstop fails a selected credentialed test whose credential never landed.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# A conftest declaring a credentialed "dbx" tier. Behavior is env-driven so one conftest serves
# every scenario: FETCH_LOG (append pid on fetch), CRED_VALID ("0" -> validate fails).
_CRED_CONFTEST = """
    import os

    from duckdb_pytest_driver import register_tier, credential


    def _fetch(config):
        with open(os.environ["FETCH_LOG"], "a") as f:
            f.write(str(os.getpid()) + "\\n")
        return {"DBX_TOKEN": "TOK"}

    def _validate(value):
        return os.environ.get("CRED_VALID", "1") == "1" and bool(value)

    def _error():
        return "DBX creds unavailable: run your env script"

    def pytest_configure(config):
        register_tier(config, "dbx", path="test/dbx", default=True,
            credentials=[credential("dbx_creds", fetch=_fetch, validate=_validate,
                         error=_error, adopt="env")])
"""


def test_credential_fetched_once_and_reaches_worker(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "1")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        import os

        from duckdb_pytest_driver import get_store, store as S


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
    assert log.read_text().count("\n") == 1  # fetch ran EXACTLY once (controller only)


def test_unreachable_tier_is_not_fetched(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "1")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test_inner.py", "def test_a(): pass")
    # -m other: the 'dbx' tier's marker doesn't match -> not reachable -> no up-front fetch.
    pytester.runpytest_subprocess("-m", "other", "-p", "no:cacheprovider")
    assert not log.exists()  # fetch never ran


def test_invalid_credential_errors_the_session(pytester, monkeypatch):
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "0")  # validate -> False
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test_inner.py", "def test_a(): pass")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0  # session aborted (UsageError, pre-fork)
    result.stderr.fnmatch_lines(["*DBX creds unavailable*"])  # the resource's own message


def test_backstop_fails_selected_credentialed_test_without_creds(pytester, monkeypatch):
    # No up-front fetch (tier unreachable via -k), but a -k-selected in-tier test still runs ->
    # the backstop must FAIL it (paradigm shift: selected-but-unprovisioned fails, never skips).
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "1")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(
        pytester,
        "test/dbx/test_live.py",  # path membership -> the 'dbx' tier
        "def test_live(): pass",
    )
    result = pytester.runpytest_subprocess("-k", "live", "-p", "no:cacheprovider")
    # A setup-phase pytest.fail is an "error" outcome (loud, red, counted) — never a skip.
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*DBX creds unavailable*"])
    assert not log.exists()  # -k did not trigger an up-front fetch


def test_vanilla_credential_free_run_unaffected(pytester, monkeypatch):
    # Sanity: with the tier present but deselected by path, no fetch and tests still run.
    log = pytester.path / "fetch.log"
    monkeypatch.setenv("FETCH_LOG", str(log))
    monkeypatch.setenv("CRED_VALID", "1")
    _write(pytester, "conftest.py", _CRED_CONFTEST)
    _write(pytester, "test/oss/test_local.py", "def test_ok(): pass")
    result = pytester.runpytest_subprocess("test/oss", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not log.exists()  # 'dbx' path doesn't intersect test/oss -> no fetch
