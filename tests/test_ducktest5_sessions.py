"""Sessions: a live engine handle that takes a test's inputs, its identity, and its lifecycle."""

import os

import pytest
from test_ducktest5_run import FakeEngine, FakeSession, go, passes, py_test

from ducktest5.binary import UsageError
from ducktest5.declare import EngineDef
from ducktest5.engines.duckdb import DuckDBSession
from ducktest5.model import Test
from ducktest5.resources import (
    RO,
    RW,
    CredentialDef,
    Opener,
    SessionDef,
    StorageDef,
    check_contract,
    duckdb_session,
    env,
    spark_session,
)
from ducktest5.scan import scan


class Live:
    """An engine handle that records what it was told, in order."""

    def __init__(self):
        self.calls, self.conf = [], {}

    def begin_test(self):
        self.calls.append("begin_test")

    def add_preamble(self, sql):
        self.calls.append(sql)

    def set_conf(self, key, value):
        self.conf[key] = value

    def close(self):
        self.calls.append("close")


def opened(definition, dialect="duckdb", inputs=None):
    """The session and every live handle it opened, newest last."""
    made = []

    def open_live(d):
        made.append(Live())
        return made[-1]

    return definition.make(inputs, opener=Opener(dialect, open_live)), made


# -----------------------------------------------------------------------------
# Identity
#


def test_identity_is_the_content_not_the_label_scope_or_timeout():
    one = SessionDef("duck", requires=["delta"])
    assert one.identity() == SessionDef("other", requires=["delta"], scope="test", timeout=5).identity()
    assert one.identity() != SessionDef("duck", requires=["httpfs"]).identity()
    assert one.identity() != SessionDef("duck", engine="spark", requires=["delta"]).identity()


def test_identity_follows_the_inputs_and_the_env_it_is_bound_to(tmp_path):
    storage = StorageDef("local", str(tmp_path))
    bound = SessionDef("duck", inputs=[storage])
    assert bound.identity({"local": "a"}) != bound.identity({"local": "b"})
    assert SessionDef("duck").identity() != SessionDef("duck").with_(env={"AWS_PROFILE": None}).identity()


def test_an_unknown_scope_is_a_usage_error():
    with pytest.raises(UsageError, match="scope 'run'"):
        SessionDef("duck", scope="run")


# -----------------------------------------------------------------------------
# Lifecycle
#


def test_acquire_opens_then_every_test_gets_a_fresh_instance_and_the_preamble_again():
    session, made = opened(SessionDef("duck", requires=["delta"]))
    for _ in range(2):
        session.release(session.acquire(RW), RW)
    assert len(made) == 1
    assert made[0].calls == ["begin_test", "LOAD delta;"] * 2


def test_after_a_failed_test_the_session_is_closed_and_opened_again():
    session, made = opened(SessionDef("duck"))
    session.release(session.acquire(RW), RW, failed=True)
    session.release(session.acquire(RW), RW)
    assert [live.calls for live in made] == [["begin_test", "close"], ["begin_test"]]


def test_a_preamble_that_fails_leaves_the_session_free_for_the_next_test():
    session, made = opened(SessionDef("duck", requires=["nope"]))
    fail = [True]

    def add_preamble(sql):
        if fail.pop():
            raise RuntimeError("no such extension")

    session.start()
    made[0].add_preamble = add_preamble
    with pytest.raises(RuntimeError):
        session.acquire(RW)
    fail.append(False)
    assert session.acquire(RW) is session


def test_session_contract():
    check_contract(lambda run_token: opened(SessionDef("duck"))[0])


# -----------------------------------------------------------------------------
# The preamble, delivered
#


@pytest.fixture
def minio_inputs(monkeypatch):
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "password")
    storage = StorageDef("minio", "s3://ducktest/delta", endpoint="http://172.18.0.1:9999")
    keys = {"AWS_ACCESS_KEY_ID": "DT_KEY", "AWS_SECRET_ACCESS_KEY": "DT_SECRET"}
    credential = CredentialDef("minio-key", sources=[env(keys)], scope="minio")
    return [storage, credential], {"minio": storage.make(), "minio-key": credential.make()}


def test_a_duckdb_session_sends_its_scoped_secret_as_sql(minio_inputs):
    defs, inputs = minio_inputs
    session, made = opened(SessionDef("duck", requires=["httpfs"], inputs=defs), "duckdb", inputs)
    session.acquire(RW)
    assert made[0].calls == [
        "begin_test",
        "LOAD httpfs;\n"
        "CREATE SECRET ducktest_minio (TYPE 's3', KEY_ID 'admin', SECRET 'password', ENDPOINT '172.18.0.1:9999', "
        "USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/delta');",
    ]


def test_a_spark_session_sets_the_buckets_settings_at_runtime(minio_inputs):
    defs, inputs = minio_inputs
    session, made = opened(SessionDef("spark", engine="spark", inputs=defs), "spark", inputs)
    session.acquire(RW)
    assert made[0].calls == ["begin_test"]
    assert made[0].conf["fs.s3a.bucket.ducktest.secret.key"] == "password"
    assert made[0].conf["fs.s3a.bucket.ducktest.endpoint"] == "http://172.18.0.1:9999"


# -----------------------------------------------------------------------------
# In a run
#

FAKE_SHELL = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_shell.py")


class SparkRecorder(FakeEngine):
    """A Spark engine that records the runtime conf it is given."""

    def __init__(self):
        super().__init__()
        self.conf = {}

    def engine(self, id="spark", uses=()):
        return EngineDef(id, "spark:fake", self._boot, dialect="spark")

    def set_conf(self, key, value):
        self.conf[key] = value


def with_minio(test, minio_inputs):
    defs, _ = minio_inputs
    return Test(test.definition, None, {}, ((defs[0], RW), (defs[1], RO)))


def test_the_default_duckdb_session_sends_the_scoped_secret_over_the_shells_stdin(tmp_path, monkeypatch, minio_inputs):
    record = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_SHELL_RECORD", str(record))

    def body(ctx):
        ctx.duckdb().oks("SELECT echo1")

    test = with_minio(py_test(body), minio_inputs)
    code, run_dir, _, _ = go(tmp_path, [test], duckdb_bin=FAKE_SHELL, session_factory=DuckDBSession, keep=True)
    assert code == 0
    sent = [line for line in record.read_text().splitlines() if not line.startswith(".print")]
    assert sent == [
        "CREATE SECRET ducktest_minio (TYPE 's3', KEY_ID 'admin', SECRET 'password', ENDPOINT '172.18.0.1:9999', "
        "USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/delta')",
        ";",
        "SELECT echo1",
        ";",
    ]
    written = [p.read_text(errors="replace") for p in run_dir.rglob("*") if p.is_file()]
    assert not any("password" in text for text in written)


def test_a_named_spark_session_gets_the_buckets_settings_and_serves_ctx_engine(tmp_path, minio_inputs):
    spark = SparkRecorder()
    seen = {}

    def body(ctx):
        seen["session"] = ctx.session("spark").exec("SELECT 1").rows
        seen["engine"] = ctx.engine("spark").exec("SELECT 2").rows

    test = with_minio(py_test(body, sessions=["spark"], engines=["spark"]), minio_inputs)
    code, _, _, _ = go(tmp_path, [test], engines=[spark.engine()], sessions=[spark_session("spark")])
    assert code == 0
    assert seen == {"session": [(1,)], "engine": [(1,)]}
    assert spark.statements == ["SELECT 1", "SELECT 2"]
    assert spark.conf["fs.s3a.bucket.ducktest.access.key"] == "admin"


def test_a_worker_session_is_reset_between_tests_and_reopened_after_a_failure(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        made.append(FakeSession(binary, home=home, env=env))
        made[-1].calls = []
        made[-1].begin_test = lambda s=made[-1]: s.calls.append("reset")
        made[-1].close = lambda s=made[-1]: s.calls.append("close")
        return made[-1]

    def fails(ctx):
        raise AssertionError("no")

    tests = [py_test(passes, path="test/a.py"), py_test(fails, path="test/b.py"), py_test(passes, path="test/c.py")]
    go(tmp_path, tests, session_factory=factory, workers=1)
    first, second = made  # the second shell is closed when the run ends
    assert (first.calls, second.calls) == (["reset", "reset", "close"], ["reset", "close"])


def test_a_test_scoped_session_is_closed_when_its_test_ends(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        made.append(FakeSession(binary, home=home, env=env))
        made[-1].closed = 0
        made[-1].close = lambda s=made[-1]: setattr(s, "closed", s.closed + 1)
        return made[-1]

    def body(ctx):
        ctx.session("own").oks("SELECT 1")

    tests = [py_test(body, path=f"test/{n}.py", sessions=["own"]) for n in "ab"]
    code, _, _, _ = go(
        tmp_path, tests, session_factory=factory, workers=1, sessions=[duckdb_session("own", scope="test")]
    )
    assert code == 0
    assert [s.closed >= 1 for s in made] == [True, True]


def test_a_session_that_cannot_open_is_a_setup_error_of_the_test(tmp_path):
    def body(ctx):
        pass

    sessions = [duckdb_session("bad", requires=["nope"])]
    factory = lambda b, **kw: FakeSession(preamble_fails=True)  # noqa: E731
    _, _, report, _ = go(tmp_path, [py_test(body, sessions=["bad"])], session_factory=factory, sessions=sessions)
    assert report["tests"][0]["outcome"] == "error"
    assert "could not be set up: SqlError: Catalog Error" in report["tests"][0]["problems"][0]


# -----------------------------------------------------------------------------
# Declared in ducktest_conf.py
#


def test_sessions_are_declared_in_the_project_and_named_by_a_test(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest5.resources import duckdb_session\nSESSIONS = [duckdb_session('duck', requires=['delta'])]\n"
    )
    (tmp_path / "t.py").write_text(
        "from ducktest5 import test\n\n@test(sessions=['duck'])\ndef a(ctx):\n    pass\n\n"
        "@test(sessions=['nope'])\ndef b(ctx):\n    pass\n"
    )
    with pytest.raises(UsageError, match="names session 'nope'; the project has \\['duck'\\]"):
        scan(str(tmp_path), working_dir=str(tmp_path))


def test_a_session_on_an_engine_the_project_lacks_is_a_usage_error(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest5.resources import spark_session\nSESSIONS = [spark_session('spark')]\n"
    )
    with pytest.raises(UsageError, match="engine 'spark', which is not in ENGINES"):
        scan(str(tmp_path), working_dir=str(tmp_path))
