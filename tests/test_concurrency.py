"""A body with threads, against a real duckdb shell: a warm client per thread, and the retry loop."""

import io
import os
import shutil

import pytest
from ducktest_real_build import missing

from ducktest.ctx import Context
from ducktest.engines.duckdb import DuckDBShell, DuckEngineDef
from ducktest.model import PYTHON, Test, TestDef
from ducktest.resources.engine import Runtime
from ducktest.run import run
from ducktest.scan import scan
from ducktest.testenv import resolve


def _shell():
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


pytestmark = pytest.mark.skipif(_shell() is None, reason="no duckdb shell (set $DUCKDB_SHELL or PATH)")


@pytest.fixture
def ctx(tmp_path):
    test = Test(TestDef(path="t.py", name="t", type=PYTHON, body=lambda ctx: None))
    home = str(tmp_path / "home")
    engine = DuckEngineDef("duck").make()
    engine.runtime = Runtime(shell=lambda env: DuckDBShell(_shell(), home=home, env=env))
    engine.start()
    context = Context(test, resolve(test), [engine], temp_dir=str(tmp_path))
    yield context
    for teardown in reversed(context.teardowns):
        teardown()
    engine.close()


def test_new_clients_are_warm_and_do_not_see_each_others_instance(ctx):
    first = ctx.client("duck", new=True)
    second = ctx.client("duck", new=True)
    for client in (first, second):
        client.oks("CREATE TABLE mine (i INT)")
    first.oks("INSERT INTO mine VALUES (1)")
    first.expects("SELECT count(*) FROM mine", [1])  # the same shell answered twice: it stayed warm
    second.expects("SELECT count(*) FROM mine", [0])
    ctx.client("duck").expects("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'mine'", [0])


@pytest.mark.skipif(
    bool(missing(_shell(), "httpfs")), reason="the build has no httpfs, which a persistent s3 secret needs"
)
def test_what_a_new_client_persists_lands_under_the_runs_private_home(ctx, tmp_path, monkeypatch):
    users_home = tmp_path / "users-home"
    users_home.mkdir()
    monkeypatch.setenv("HOME", str(users_home))
    ctx.client("duck", new=True).oks("CREATE PERSISTENT SECRET probe (TYPE s3, KEY_ID 'k', SECRET 'x')")
    assert (tmp_path / "home" / ".duckdb" / "stored_secrets" / "probe.duckdb_secret").exists()
    assert list(users_home.iterdir()) == []


# -----------------------------------------------------------------------------
# The whole thing: a retry loop over threads, run as a project
#

PROOF = os.path.join(os.path.dirname(__file__), "ducktest_fakes", "concurrent_rw.py")


def proof(tmp_path, mutate=lambda text: text):
    """Run the concurrent-writers test as its own project; `mutate` breaks it on purpose."""
    root = tmp_path / "test"
    root.mkdir()
    (root / "ducktest_conf.py").write_text("from ducktest import Project\nproject = Project()\n")
    with open(PROOF) as f:
        (root / "concurrent_rw.py").write_text(mutate(f.read()))
    tests, project = scan(str(root), working_dir=str(tmp_path))
    printed = io.StringIO()
    code = run(
        tests, project, duckdb_bin=_shell(), working_dir=str(tmp_path), temp_root=str(tmp_path / "runs"), out=printed
    )
    return code, printed.getvalue()


def test_every_append_gets_in_and_the_ids_have_no_gap(tmp_path):
    code, printed = proof(tmp_path)
    assert code == 0, printed
    assert "PASSED   concurrent_rw.py::concurrent_writers_append_without_a_gap" in printed


def test_a_wrong_final_list_shows_both_sides_and_the_diff(tmp_path):
    code, printed = proof(tmp_path, lambda text: text.replace("range(1, len(ids) + 1)", "range(2, len(ids) + 2)"))
    assert code == 1
    assert "assert ids == list(range(2, len(ids) + 2))" in printed
    assert "diff, - left + right:" in printed
    assert "concurrent_rw.py:58" in printed  # the line the assert is on, not the one it was raised from


def test_an_error_the_body_did_not_expect_is_reported_with_the_statement(tmp_path):
    # Nothing detaches the file, so every writer's first ATTACH hits the lock -- and the retry
    # loop no longer takes a lock error for a reason to come back.
    def break_it(text):
        return text.replace("        DETACH shared\n", "").replace('"lock" not in', '"cosmic ray" not in')

    code, printed = proof(tmp_path, break_it)
    assert code == 1
    assert "ATTACH failed for a reason this test does not expect" in printed
    assert "[duck] exec, failed: IO Error: Could not set lock on file" in printed
    assert "ATTACH '" in printed and "shared.db' AS shared" in printed
