"""Self-tests for the out-of-session service lifecycle commands (P2): --provision-service /
--teardown-service. Offline: FAKE start/stop/alive that log to files; the commands exit at
pytest_configure (before collection), so no binary is needed.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# A fake service: start/stop append their pid to logs; alive() is toggled by env; attach() echoes.
_CONFTEST = """
    import os
    from ducktest import register_suite, service

    def _start(config):
        with open(os.environ["START_LOG"], "a") as f:
            f.write("start\\n")
        return {"endpoint": "svc://up"}

    def _stop(config):
        with open(os.environ["STOP_LOG"], "a") as f:
            f.write("stop\\n")

    def _attach(overrides, config):
        return {"endpoint": overrides.get("endpoint", "svc://default")}

    def _alive(block):
        return os.environ.get("ALIVE", "0") == "1"

    def pytest_configure(config):
        register_suite(config, "svc_suite", default=True, services=[
            service("dummy", start=_start, stop=_stop, attach=_attach, alive=_alive, fixture="f")])
"""


def test_provision_starts_and_leaves_running(pytester, monkeypatch):
    start_log = pytester.path / "start.log"
    stop_log = pytester.path / "stop.log"
    monkeypatch.setenv("START_LOG", str(start_log))
    monkeypatch.setenv("STOP_LOG", str(stop_log))
    monkeypatch.setenv("ALIVE", "0")  # not already up -> must start
    _write(pytester, "conftest.py", _CONFTEST)
    result = pytester.runpytest_subprocess("--provision-service", "dummy", "-p", "no:cacheprovider")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*dummy: up at svc://up*", "*attach: --existing-service dummy=svc://up*"])
    assert start_log.read_text().count("start") == 1  # started once
    assert not stop_log.exists()  # LEFT running — sessionfinish must not tear it down


def test_provision_idempotent_skips_when_alive(pytester, monkeypatch):
    start_log = pytester.path / "start.log"
    monkeypatch.setenv("START_LOG", str(start_log))
    monkeypatch.setenv("STOP_LOG", str(pytester.path / "stop.log"))
    monkeypatch.setenv("ALIVE", "1")  # already up -> skip
    _write(pytester, "conftest.py", _CONFTEST)
    result = pytester.runpytest_subprocess("--provision-service", "dummy", "-p", "no:cacheprovider")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*dummy: already running*skipped*"])
    assert not start_log.exists()  # never started


def test_teardown_stops(pytester, monkeypatch):
    stop_log = pytester.path / "stop.log"
    monkeypatch.setenv("START_LOG", str(pytester.path / "start.log"))
    monkeypatch.setenv("STOP_LOG", str(stop_log))
    _write(pytester, "conftest.py", _CONFTEST)
    result = pytester.runpytest_subprocess("--teardown-service", "dummy", "-p", "no:cacheprovider")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*dummy: stopped*"])
    assert stop_log.read_text().count("stop") == 1


def test_provision_key_filter_only_targets_named(pytester, monkeypatch):
    a_log = pytester.path / "a.log"
    b_log = pytester.path / "b.log"
    monkeypatch.setenv("A_LOG", str(a_log))
    monkeypatch.setenv("B_LOG", str(b_log))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        from ducktest import register_suite, service

        def _mk(logvar):
            def _start(config):
                open(os.environ[logvar], "a").write("x")
                return {"endpoint": "svc://" + logvar}
            return _start

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("alpha", start=_mk("A_LOG"), fixture="fa"),
                service("beta", start=_mk("B_LOG"), fixture="fb")])
        """,
    )
    result = pytester.runpytest_subprocess("--provision-service", "beta", "-p", "no:cacheprovider")
    assert result.ret == 0
    assert b_log.exists() and not a_log.exists()  # only the named service started
