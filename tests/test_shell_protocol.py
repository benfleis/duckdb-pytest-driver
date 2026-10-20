"""The protocol between ducktest's DuckDB shell and the shell, against a fake shell: time limits and
threads. No DuckDB is needed."""

import logging
import os
import threading
import time

import pytest

from ducktest.resources.client import SqlError
from ducktest.engines.duckdb import DuckDBShell
from ducktest.engines.spark import SparkHandle

FAKE_SHELL = os.path.join(os.path.dirname(__file__), "ducktest_fakes", "fake_shell.py")


@pytest.fixture
def shell():
    s = DuckDBShell(FAKE_SHELL, timeout=5)
    yield s
    s.close()


def test_a_statement_that_still_prints_at_its_time_limit_is_killed(shell):
    t0 = time.monotonic()
    with pytest.raises(SqlError, match="not done in 0.5s; the duckdb shell was killed"):
        shell.run(["SELECT drip"], timeout=0.5)
    assert time.monotonic() - t0 < 3
    assert shell.run(["SELECT echo4"]) == (["v"], [(4,)])  # a new shell, at the next statement


def test_a_shell_that_died_under_a_test_is_opened_again_and_the_log_says_so(shell, caplog):
    shell.label, shell.holder = "duck: duck-shell", "t.py::t"
    assert shell.run(["SELECT echo1"]) == (["v"], [(1,)])
    shell._process.kill()
    shell._process.wait()
    with caplog.at_level(logging.INFO, logger="ducktest.engines.duckdb"):
        assert shell.run(["SELECT echo2"]) == (["v"], [(2,)])
    assert "duck: duck-shell died under t.py::t; opened again" in caplog.messages


def test_a_shell_that_ducktest_stopped_for_a_statement_timeout_is_logged_as_stopped(shell, caplog):
    shell.label, shell.holder = "duck: duck-shell", "t.py::t"
    with pytest.raises(SqlError, match="not done in 0.5s"):
        shell.run(["SELECT drip"], timeout=0.5)
    with caplog.at_level(logging.INFO, logger="ducktest.engines.duckdb"):
        assert shell.run(["SELECT echo2"]) == (["v"], [(2,)])
    assert caplog.messages == ["duck: duck-shell stopped under t.py::t (statement timeout); opened again"]


def test_a_shell_that_could_not_open_a_fresh_instance_is_logged_as_stopped(shell, caplog, monkeypatch):
    shell.label, shell.holder = "duck: duck-shell", "t.py::t"
    assert shell.run(["SELECT echo1"]) == (["v"], [(1,)])

    def fails(command):
        raise SqlError(f"{command}: no")

    monkeypatch.setattr(shell, "_dot", fails)
    shell.begin_test()
    with caplog.at_level(logging.INFO, logger="ducktest.engines.duckdb"):
        assert shell.run(["SELECT echo2"]) == (["v"], [(2,)])
    assert caplog.messages == ["duck: duck-shell stopped under t.py::t (open failed); opened again"]


def test_a_shell_that_died_while_no_test_held_it_is_logged_as_died_between_tests(shell, caplog):
    shell.label, shell.holder = "duck: duck-shell", "t.py::t"
    assert shell.run(["SELECT echo1"]) == (["v"], [(1,)])
    shell.holder = None
    shell._process.kill()
    shell._process.wait()
    shell.holder = "t.py::next"
    shell.begin_test()
    with caplog.at_level(logging.INFO, logger="ducktest.engines.duckdb"):
        assert shell.run(["SELECT echo2"]) == (["v"], [(2,)])
    assert caplog.messages == ["duck: duck-shell died between tests; opened again"]


def test_two_threads_never_get_each_others_result(shell):
    wrong = []

    def ask(n):
        try:
            for _ in range(20):
                assert shell.run([f"SELECT echo{n}"])[1] == [(n,)]
        except BaseException as exc:
            wrong.append(exc)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wrong == []


def test_spark_with_no_statement_gives_no_rows():
    assert SparkHandle(session=None).run([]) == ([], [])
