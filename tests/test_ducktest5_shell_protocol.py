"""The protocol between ducktest5's DuckDB session and the shell, against a fake shell: time limits and
threads. No DuckDB is needed."""

import os
import threading
import time

import pytest

from ducktest5.client import SqlError
from ducktest5.engines.duckdb import DuckDBSession
from ducktest5.engines.spark import SparkHandle

FAKE_SHELL = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_shell.py")


@pytest.fixture
def session():
    s = DuckDBSession(FAKE_SHELL, timeout=5)
    yield s
    s.close()


def test_a_statement_that_still_prints_at_its_time_limit_is_killed(session):
    t0 = time.monotonic()
    with pytest.raises(SqlError, match="not done in 0.5s; the duckdb shell was killed"):
        session.run(["SELECT drip"], timeout=0.5)
    assert time.monotonic() - t0 < 3
    assert session.run(["SELECT echo4"]) == (["v"], [(4,)])  # a new shell, at the next statement


def test_two_threads_never_get_each_others_result(session):
    wrong = []

    def ask(n):
        try:
            for _ in range(20):
                assert session.run([f"SELECT echo{n}"])[1] == [(n,)]
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
