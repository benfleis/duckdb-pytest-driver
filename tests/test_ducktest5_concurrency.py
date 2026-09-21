"""A body with threads, against a real duckdb shell: a warm client per thread, and the retry loop."""

import os
import shutil

import pytest

from ducktest5.ctx import Ctx
from ducktest5.engines.duckdb import DuckDBSession
from ducktest5.model import PYTHON, Case, TestDef


def _shell():
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


pytestmark = pytest.mark.skipif(_shell() is None, reason="no duckdb shell (set $DUCKDB_SHELL or PATH)")


@pytest.fixture
def ctx(tmp_path):
    case = Case(TestDef(path="t.py", name="t", type=PYTHON, body=lambda ctx: None), None, {})
    home = str(tmp_path / "home")
    shared = DuckDBSession(_shell(), home=home)
    shared.begin_test()
    context = Ctx(
        case, temp_dir=str(tmp_path), duckdb_bin=_shell(), duckdb_session=shared, duckdb_home=home, engines={}
    )
    yield context
    for teardown in reversed(context.teardowns):
        teardown()
    shared.close()


def test_own_clients_are_warm_and_do_not_see_each_others_instance(ctx):
    first = ctx.duckdb("CREATE TABLE mine (i INT);", session="own")
    second = ctx.duckdb("CREATE TABLE mine (i INT);", session="own")
    first.oks("INSERT INTO mine VALUES (1)")
    first.expects("SELECT count(*) FROM mine", [1])  # the same shell answered twice: it stayed warm
    second.expects("SELECT count(*) FROM mine", [0])
    ctx.duckdb().expects("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'mine'", [0])


def test_what_an_own_client_persists_lands_under_the_runs_private_home(ctx, tmp_path, monkeypatch):
    users_home = tmp_path / "users-home"
    users_home.mkdir()
    monkeypatch.setenv("HOME", str(users_home))
    ctx.duckdb(session="own").oks("CREATE PERSISTENT SECRET probe (TYPE s3, KEY_ID 'k', SECRET 'x')")
    assert (tmp_path / "home" / ".duckdb" / "stored_secrets" / "probe.duckdb_secret").exists()
    assert list(users_home.iterdir()) == []
