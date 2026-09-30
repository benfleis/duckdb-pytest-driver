"""Engines and the Session of a test: an engine instance takes a test's inputs, has an identity and
a lifecycle, and mints the Clients a test's Session holds."""

import os
from types import SimpleNamespace

import pytest
from test_ducktest5_run import FakeEngine, FakeSession, ctx_for, go, passes, py_test, sql_test

from ducktest5 import phases, sessions
from ducktest5.binary import UsageError
from ducktest5.cli import main
from ducktest5.conf import Project
from ducktest5.declare import Variant
from ducktest5.engines.duckdb import DuckDBSession, duckdb_engine
from ducktest5.model import SQLLOGIC, Test
from ducktest5.resources import RO, RW, BootedEngineDef, CredentialDef, StorageDef, check_contract, from_env
from ducktest5.resources.engine import Runtime
from ducktest5.scan import scan
from ducktest5.sqllogic import init_file_text
from ducktest5.testenv import resolve


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


def opened(definition, inputs=None):
    """The engine instance and every live handle it opened, newest last."""
    made = []

    def open_live(*_):
        made.append(Live())
        return made[-1]

    engine = definition.make(inputs)
    engine.runtime = Runtime(shell=open_live, booted={definition.id: SimpleNamespace(for_worker=open_live)})
    return engine, made


# -----------------------------------------------------------------------------
# Identity
#


def test_identity_is_the_content_not_the_label_scope_or_timeout():
    one = duckdb_engine("duck", requires=["delta"])
    assert one.identity() == duckdb_engine("other", requires=["delta"], scope="run", timeout=5).identity()
    assert one.identity() != duckdb_engine("duck", requires=["httpfs"]).identity()
    assert one.identity() != BootedEngineDef("duck", requires=["delta"]).identity()


def test_identity_follows_the_inputs_and_the_env_it_is_bound_to(tmp_path):
    storage = StorageDef("local", str(tmp_path))
    bound = duckdb_engine("duck", inputs=[storage])
    assert bound.identity({"local": "a"}) != bound.identity({"local": "b"})
    assert duckdb_engine("duck").identity() != duckdb_engine("duck").with_(env={"AWS_PROFILE": None}).identity()


def test_an_unknown_scope_is_a_usage_error():
    with pytest.raises(UsageError, match="scope 'test'"):
        duckdb_engine("duck", scope="test")


# -----------------------------------------------------------------------------
# Lifecycle
#


def test_acquire_opens_then_every_test_gets_a_fresh_instance_and_the_preamble_again():
    session, made = opened(duckdb_engine("duck", requires=["delta"]))
    for _ in range(2):
        session.release(session.acquire(RW), RW)
    assert len(made) == 1
    assert made[0].calls == ["begin_test", "LOAD delta;"] * 2


def test_after_a_failed_test_the_session_is_closed_and_opened_again():
    session, made = opened(duckdb_engine("duck"))
    session.release(session.acquire(RW), RW, failed=True)
    session.release(session.acquire(RW), RW)
    assert [live.calls for live in made] == [["begin_test", "close"], ["begin_test"]]


def test_a_preamble_that_fails_leaves_the_session_free_for_the_next_test():
    session, made = opened(duckdb_engine("duck", requires=["nope"]))
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


def test_engine_contract():
    check_contract(lambda run_token: opened(duckdb_engine("duck"))[0])


# -----------------------------------------------------------------------------
# The preamble, delivered
#


@pytest.fixture
def minio_inputs(monkeypatch):
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "password")
    storage = StorageDef("minio", "s3://ducktest/delta", endpoint="http://172.18.0.1:9999")
    keys = {"DT_KEY": "AWS_ACCESS_KEY_ID", "DT_SECRET": "AWS_SECRET_ACCESS_KEY"}
    credential = CredentialDef("minio-key", sources=[from_env(keys)], scopes=["minio"])
    return [storage, credential], {"minio": storage.make(), "minio-key": credential.make()}


def test_a_duckdb_engine_sends_its_scoped_secret_as_sql(minio_inputs):
    defs, inputs = minio_inputs
    session, made = opened(duckdb_engine("duck", requires=["httpfs"], inputs=defs), inputs)
    session.acquire(RW)
    assert made[0].calls == [
        "begin_test",
        "LOAD httpfs;\n"
        "CREATE SECRET ducktest_minio (TYPE 's3', KEY_ID 'admin', SECRET 'password', ENDPOINT '172.18.0.1:9999', "
        "USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/delta');",
    ]


def test_a_spark_engine_sets_the_buckets_settings_at_runtime(minio_inputs):
    defs, inputs = minio_inputs
    session, made = opened(BootedEngineDef("spark", dialect="spark", inputs=defs), inputs)
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

    def engine(self, id="spark"):
        return BootedEngineDef(id, version="spark:fake", boot=self._boot, dialect="spark")

    def set_conf(self, key, value):
        self.conf[key] = value


def with_minio(test, minio_inputs):
    defs, _ = minio_inputs
    return Test(test.definition, None, {}, ((defs[0], RW), (defs[1], RO)))


def test_the_default_duckdb_engine_sends_the_scoped_secret_over_the_shells_stdin(tmp_path, monkeypatch, minio_inputs):
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


def test_a_spark_client_is_one_per_test_and_its_engine_gets_the_buckets_settings(tmp_path, minio_inputs):
    spark = SparkRecorder()
    seen = {}

    def body(ctx):
        seen["first"] = ctx.client("spark").exec("SELECT 1").rows
        seen["same"] = ctx.client("spark") is ctx.session.client("spark")

    test = with_minio(py_test(body, clients=["spark"], engines=["spark"]), minio_inputs)
    code, _, _, _ = go(tmp_path, [test], engines=[spark.engine()])
    assert code == 0
    assert seen == {"first": [(1,)], "same": True}
    assert spark.statements == ["SELECT 1"]
    assert spark.conf["fs.s3a.bucket.ducktest.access.key"] == "admin"


def test_a_worker_engine_is_reset_between_tests_and_reopened_after_a_failure(tmp_path):
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


def test_a_batch_scoped_engine_is_closed_when_its_python_test_ends(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        made.append(FakeSession(binary, home=home, env=env))
        made[-1].closed = 0
        made[-1].close = lambda s=made[-1]: setattr(s, "closed", s.closed + 1)
        return made[-1]

    def body(ctx):
        ctx.client("own").oks("SELECT 1")

    tests = [py_test(body, path=f"test/{n}.py", clients=["own"]) for n in "ab"]
    code, _, _, _ = go(
        tmp_path, tests, session_factory=factory, workers=1, engines=[duckdb_engine("own", scope="batch")]
    )
    assert code == 0
    assert [s.closed >= 1 for s in made] == [True, True]


def test_an_engine_that_cannot_open_is_a_setup_error_of_the_test(tmp_path):
    def body(ctx):
        pass

    engines = [duckdb_engine("bad", requires=["nope"])]
    factory = lambda b, **kw: FakeSession(preamble_fails=True)  # noqa: E731
    _, _, report, _ = go(tmp_path, [py_test(body, clients=["bad"])], session_factory=factory, engines=engines)
    assert report["tests"][0]["outcome"] == "error"
    assert "could not be set up: SqlError: Catalog Error" in report["tests"][0]["problems"][0]


def test_an_engine_of_scope_run_is_one_instance_for_the_run(tmp_path):
    clones = []

    class Cloning(FakeEngine):
        def for_worker(self):
            clones.append(1)
            return self

    engine = Cloning()
    tests = [py_test(passes, path=f"test/{n}.py", engines=["fake"]) for n in "abcd"]
    code, _, _, _ = go(tmp_path, tests, engines=[engine.engine().with_(scope="run")], workers=4)
    assert (code, len(clones), engine.calls.count("begin_test")) == (0, 1, 4)


# -----------------------------------------------------------------------------
# The Session of a test
#


def test_a_value_bound_on_the_session_reaches_every_client_and_one_bound_on_a_client_only_that_one(tmp_path):
    fake = FakeEngine()
    ctx = ctx_for(py_test(passes, engines=["fake"]), tmp_path, engine=fake)
    ctx.session.bind(path="/p")
    ctx.client("fake", only="x").oks("SELECT '{path}', '{only}'")
    ctx.client("duckdb").oks("SELECT '{path}'")
    with pytest.raises(UsageError, match="no value for {only}"):
        ctx.client("duckdb").oks("SELECT '{only}'")
    assert fake.statements == ["SELECT '/p', 'x'"]
    assert ctx.session.duckdb.engine.live.statements == ["SELECT '/p'"]


def test_a_session_holds_one_client_per_engine_and_its_identity_is_theirs_and_the_environment(tmp_path):
    ctx = ctx_for(py_test(passes, engines=["fake"]), tmp_path)
    assert ctx.client("fake") is ctx.client("fake", table="t") is ctx.session.client("fake")
    other = ctx_for(py_test(passes), tmp_path)
    assert ctx.session.identity != other.session.identity
    assert other.session.identity == ctx_for(py_test(passes), tmp_path).session.identity


# -----------------------------------------------------------------------------
# Declared in ducktest_conf.py
#


def test_engines_are_declared_among_the_services_and_named_by_a_test(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest5.engines.duckdb import duckdb_engine\nSERVICES = [duckdb_engine('duck', requires=['delta'])]\n"
    )
    (tmp_path / "t.py").write_text(
        "from ducktest5 import test\n\n@test(clients=['duck'])\ndef a(ctx):\n    pass\n\n"
        "@test(clients=['nope'])\ndef b(ctx):\n    pass\n"
    )
    with pytest.raises(UsageError, match="names client 'nope'; the project's engines are \\['duck'\\]"):
        scan(str(tmp_path), working_dir=str(tmp_path))


@pytest.mark.parametrize(
    "conf, text",
    [
        ("SESSIONS = []", "declare it among SERVICES, not in SESSIONS"),
        ("from ducktest5 import default\nRULES = [default(sessions=['duck'])]", "sessions= is now clients="),
    ],
)
def test_sessions_are_not_declared(tmp_path, conf, text):
    (tmp_path / "ducktest_conf.py").write_text(conf)
    with pytest.raises(UsageError, match=text):
        scan(str(tmp_path), working_dir=str(tmp_path))


# -----------------------------------------------------------------------------
# A .test batch is a Session
#

ENGINES_CONF = """
from ducktest5 import match
from ducktest5.engines.duckdb import duckdb_engine

SERVICES = [
    duckdb_engine("delta", requires=["delta"]),
    duckdb_engine("json", requires=["json"]),
    duckdb_engine("also_json", requires=["json"]),
]
RULES = [
    match("delta.test", clients=["delta"]),
    match("json.test", clients=["json"]),
    match("also_json.test", clients=["also_json"]),
    match("inline_require.test", clients=["json"]),
]
"""


@pytest.fixture
def bound(tmp_path, monkeypatch):
    """A project whose `.test` files a rule each binds to an engine."""
    (tmp_path / "ducktest_conf.py").write_text(ENGINES_CONF)
    for name in ("delta", "json", "also_json"):
        (tmp_path / f"{name}.test").write_text("query I\nSELECT 1\n----\n1\n")
    (tmp_path / "inline_require.test").write_text("require json\n\nquery I\nSELECT 1\n----\n1\n")
    (tmp_path / "body.py").write_text(
        "from ducktest5 import test\n\n@test(clients=['json'])\ndef body(ctx):\n    pass\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def batch_of_each_file(tmp_path):
    tests, project = scan(str(tmp_path), working_dir=str(tmp_path))
    plan = phases.build_plan([c for c in tests if c.definition.type == SQLLOGIC], project)
    return {f: b["id"] for b in plan["batches"] for f in b["files"]}


def test_test_files_share_a_batch_exactly_when_their_engines_have_one_identity(bound):
    batch = batch_of_each_file(bound)
    assert batch["delta.test"] != batch["json.test"]
    # two names for one content are one engine; an inline `require` is part of the body, not of it
    assert batch["json.test"] == batch["also_json.test"] == batch["inline_require.test"]


def test_a_test_file_has_one_client_on_the_unittest_engine_of_its_batch(bound):
    tests, project = scan(str(bound), working_dir=str(bound))
    of = {
        t.definition.path: sessions.batch_session(sessions.environment(t, resolve(t), project))
        for t in tests
        if t.definition.type == SQLLOGIC
    }
    ((name, client),) = of["json.test"].clients.items()
    assert (name, client.dialect, client.engine.definition.scope) == ("unittest", "unittest", "batch")
    assert of["json.test"].identity == of["also_json.test"].identity != of["delta.test"].identity


def test_scan_env_shows_the_clients_of_each_test_and_what_their_engines_render(bound, capsys):
    assert main(["scan", "--env"]) == 0
    printed = capsys.readouterr().out
    assert (
        "delta.test::delta\n  client: delta (unittest)  <- SERVICES delta\n    require: delta  <- engine delta\n"
        in printed
    )
    assert "body.py::body\n  client: json (duckdb)  <- SERVICES json\n    require: json  <- engine json\n" in printed


def test_the_engine_comes_first_in_the_init_file_and_nothing_is_given_twice():
    duck = duckdb_engine("duck", requires=["httpfs", "json"], settings={"threads": 2, "memory_limit": "1GB"})
    project = Project("/p", {"duck": duck}, {}, {}, {}, ())
    test = sql_test(
        "a.test", {"storage": Variant("s3", requires=["httpfs"])}, clients=["duck"], settings={"threads": 4}
    )
    env = sessions.environment(test, resolve(test), project)
    text, _ = init_file_text(env)
    assert text == (
        "require json\n\nrequire httpfs\n\n"
        "statement ok\nSET memory_limit = '1GB';\n\n"
        "statement ok\nSET threads = '4';\n"
    )


def test_the_batch_of_a_test_file_is_given_its_engine(tmp_path):
    (tmp_path / "a.test").write_text("query I\nSELECT 1\n----\n1\n")
    test = sql_test("a.test", clients=["duck"])
    _, run_dir, _, _ = go(tmp_path, [test], engines=[duckdb_engine("duck", requires=["delta"])], keep=True)
    (init,) = run_dir.glob("batches/*/init.sqllogic")
    assert init.read_text() == "require delta\n"


def test_a_test_file_runs_on_one_engine(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest5 import default\nfrom ducktest5.engines.duckdb import duckdb_engine\n"
        "SERVICES = [duckdb_engine('a'), duckdb_engine('b')]\nRULES = [default(clients=['a', 'b'])]\n"
    )
    (tmp_path / "x.test").write_text("query I\nSELECT 1\n----\n1\n")
    with pytest.raises(UsageError, match=r"names clients \['a', 'b'\]; a .test body runs on one engine"):
        scan(str(tmp_path), working_dir=str(tmp_path))
