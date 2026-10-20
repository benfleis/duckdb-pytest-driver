"""Engines and the Context of a test: an engine instance takes a test's inputs, has an identity and
a lifecycle, and mints the Clients a test's Context holds."""

import logging
import os
import re
from types import SimpleNamespace

import pytest
from test_run import FakeEngine, FakeShell, ctx_for, go, matrix_value, passes, py_test, sql_test

from ducktest import leases, phases
from ducktest.binary import UsageError
from ducktest.cli import main
from ducktest.conf import LoadedProject
from ducktest.engines.duckdb import DuckDBShell, DuckEngineDef
from ducktest.engines.spark import SparkHandle, env_provider
from ducktest.engines.unittest import UnittestEngineDef
from ducktest.model import PYTHON, SQLLOGIC, Test, TestDef, Value
from ducktest.options import Options
from ducktest.resources import RO, RW, BootedEngineDef, S3CredentialDef, StorageDef, check_contract, from_env
from ducktest.resources.client import SqlError
from ducktest.resources.engine import Runtime
from ducktest.resources.made import planned_identity
from ducktest.scan import scan
from ducktest.sqllogic import init_file_text
from ducktest.testenv import resolve


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
    engine.runtime = Runtime(shell=open_live, booted={definition.name: SimpleNamespace(for_worker=open_live)})
    return engine, made


# -----------------------------------------------------------------------------
# Identity
#


def test_identity_is_the_content_not_the_label_lifetime_or_statement_timeout():
    one = DuckEngineDef("duck", requires=["delta"])
    assert (
        one.identity() == DuckEngineDef("other", requires=["delta"], lifetime="run", statement_timeout=5).identity()
    )
    assert one.identity() != DuckEngineDef("duck", requires=["httpfs"]).identity()
    assert one.identity() != BootedEngineDef("duck", requires=["delta"]).identity()


def test_identity_follows_the_inputs_and_the_env_it_is_bound_to(tmp_path):
    storage = StorageDef("local", str(tmp_path))
    bound = DuckEngineDef("duck", inputs=[storage])
    assert bound.identity({"local": "a"}) != bound.identity({"local": "b"})
    assert (
        DuckEngineDef("duck").identity()
        != DuckEngineDef("duck").with_(process_env={"AWS_PROFILE": None}).identity()
    )


def test_an_unknown_lifetime_is_a_usage_error():
    with pytest.raises(UsageError, match="lifetime 'test'"):
        DuckEngineDef("duck", lifetime="test")


# -----------------------------------------------------------------------------
# Lifecycle
#


def test_acquire_opens_then_every_test_gets_a_fresh_instance_and_the_preamble_again():
    engine, made = opened(DuckEngineDef("duck", requires=["delta"]))
    for _ in range(2):
        engine.release(engine.acquire(RW), RW)
    assert len(made) == 1
    assert made[0].calls == ["begin_test", "LOAD delta;"] * 2


def test_after_a_failed_test_the_engine_is_closed_and_opened_again():
    engine, made = opened(DuckEngineDef("duck"))
    engine.release(engine.acquire(RW), RW, failed=True)
    engine.release(engine.acquire(RW), RW)
    assert [live.calls for live in made] == [["begin_test", "close"], ["begin_test"]]


def test_a_preamble_that_fails_leaves_the_engine_free_for_the_next_test():
    engine, made = opened(DuckEngineDef("duck", requires=["nope"]))
    fail = [True]

    def add_preamble(sql):
        if fail.pop():
            raise RuntimeError("no such extension")

    engine.start()
    made[0].add_preamble = add_preamble
    with pytest.raises(RuntimeError):
        engine.acquire(RW)
    fail.append(False)
    assert engine.acquire(RW) is engine


def test_engine_contract():
    check_contract(lambda run_token: opened(DuckEngineDef("duck"))[0])


class FakeSparkSession:
    """What a Spark handle asks of its session: `SET` lists the configuration, `conf` changes it."""

    def __init__(self, conf):
        self.settings = dict(conf)
        self.conf = SimpleNamespace(set=self.settings.__setitem__, unset=lambda key: self.settings.pop(key))
        self.catalog = SimpleNamespace(listTables=lambda: [], clearCache=lambda: None)

    def sql(self, statement):
        assert statement == "SET"
        return SimpleNamespace(collect=lambda: list(self.settings.items()))


def test_a_spark_worker_s_configuration_goes_back_to_what_it_opened_with_before_each_test():
    session = FakeSparkSession({"spark.sql.shuffle.partitions": "2"})
    handle = SparkHandle(session)
    handle._conf = handle._current_conf()  # as for_worker() does on the clone
    session.settings.update({"spark.sql.shuffle.partitions": "8", "spark.fixups.leak": "yes"})
    handle.begin_test()
    assert session.settings == {"spark.sql.shuffle.partitions": "2"}


# -----------------------------------------------------------------------------
# The preamble, delivered
#


@pytest.fixture
def remote_inputs(monkeypatch):
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "password")
    storage = StorageDef("remote", "s3://ducktest/delta", endpoint="http://172.18.0.1:9999")
    keys = {"DT_KEY": "key_id", "DT_SECRET": "secret"}
    credential = S3CredentialDef("remote-key", sources=[from_env(keys)], scopes=["remote"])
    return [storage, credential], {"remote": storage.make(), "remote-key": credential.make()}


def test_a_duckdb_engine_sends_its_scoped_secret_as_sql(remote_inputs):
    defs, inputs = remote_inputs
    engine, made = opened(DuckEngineDef("duck", requires=["httpfs"], inputs=defs), inputs)
    engine.acquire(RW)
    assert made[0].calls == [
        "begin_test",
        "LOAD httpfs;\n"
        "CREATE SECRET ducktest_remote_key (TYPE s3, PROVIDER credential_chain, CHAIN 'env', "
        "ENDPOINT '172.18.0.1:9999', URL_STYLE 'path', USE_SSL false, SCOPE 's3://ducktest/delta');",
    ]


def test_a_shell_without_the_aws_extension_says_the_s3_credential_needs_it(remote_inputs):
    class NoAws(Live):
        def add_preamble(self, sql):
            # What a build answers that has no aws in its repository, when CREATE SECRET autoloads it.
            raise SqlError(
                "Extension Autoloading Error: An error occurred while trying to automatically install the "
                "required extension 'aws:\nFailed to install local extension \"aws\""
            )

    defs, inputs = remote_inputs
    engine, _ = opened(DuckEngineDef("duck", inputs=defs), inputs)
    engine.runtime = Runtime(shell=lambda *_: NoAws())
    with pytest.raises(SqlError, match=r"(?s)^S3CredentialDef\('remote-key'\) needs the aws extension: .*Autoloading"):
        engine.acquire(RW)


def test_a_spark_engine_sets_the_buckets_settings_at_runtime(remote_inputs):
    defs, inputs = remote_inputs
    engine, made = opened(BootedEngineDef("spark", dialect="spark", inputs=defs), inputs)
    engine.acquire(RW)
    assert made[0].calls == ["begin_test"]
    assert made[0].conf["fs.s3a.bucket.ducktest.aws.credentials.provider"] == env_provider()
    assert made[0].conf["fs.s3a.bucket.ducktest.endpoint"] == "http://172.18.0.1:9999"
    assert not any("password" in v for v in made[0].conf.values())


# -----------------------------------------------------------------------------
# In a run
#

FAKE_SHELL = os.path.join(os.path.dirname(__file__), "ducktest_fakes", "fake_shell.py")


class SparkRecorder(FakeEngine):
    """A Spark engine that records the runtime conf it is given."""

    def __init__(self):
        super().__init__()
        self.conf = {}

    def engine(self, name="spark"):
        return BootedEngineDef(name, version="spark:fake", boot=self._boot, dialect="spark")

    def set_conf(self, key, value):
        self.conf[key] = value


def with_remote(test, remote_inputs):
    defs, _ = remote_inputs
    out = Test(test.definition, {}, ((defs[0], RW), (defs[1], RO)))
    out.roles = {"storage": (defs[0], None), "temp": (defs[0], None)}
    return out


def test_the_log_names_the_engine_and_the_process_kind_of_a_shell_it_opens_for_a_test(tmp_path, caplog):
    def body(ctx):
        ctx.client("duck").oks("SELECT echo1")

    with caplog.at_level(logging.INFO):
        code, _, _, _ = go(tmp_path, [py_test(body)], duckdb_bin=FAKE_SHELL, shell_factory=DuckDBShell)
    assert code == 0
    assert "duck: duck-shell opened for test/body.py::body" in caplog.messages


def test_the_default_duckdb_engine_sends_the_scoped_secret_over_the_shells_stdin(tmp_path, monkeypatch, remote_inputs):
    record = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_SHELL_RECORD", str(record))

    def body(ctx):
        ctx.client("duck").oks("SELECT echo1")

    test = with_remote(py_test(body), remote_inputs)
    code, run_dir, _, _ = go(tmp_path, [test], duckdb_bin=FAKE_SHELL, shell_factory=DuckDBShell, keep=True)
    assert code == 0
    sent = [line for line in record.read_text().splitlines() if not line.startswith(".print")]
    assert sent == [
        "CREATE SECRET ducktest_remote_key (TYPE s3, PROVIDER credential_chain, CHAIN 'env', "
        "ENDPOINT '172.18.0.1:9999', URL_STYLE 'path', USE_SSL false, SCOPE 's3://ducktest/delta')",
        ";",
        "SELECT echo1",
        ";",
    ]
    written = [p.read_text(errors="replace") for p in run_dir.rglob("*") if p.is_file()]
    assert not any("password" in text for text in written)


def test_a_spark_client_is_one_per_test_and_its_engine_gets_the_buckets_settings(tmp_path, remote_inputs):
    spark = SparkRecorder()
    seen = {}

    def body(ctx):
        seen["first"] = ctx.client("spark").exec("SELECT 1").rows
        seen["same"] = ctx.client("spark") is ctx.clients["spark"]

    test = with_remote(py_test(body, engines=["spark"]), remote_inputs)
    code, _, _, _ = go(tmp_path, [test], engines=[spark.engine()])
    assert code == 0
    assert seen == {"first": [(1,)], "same": True}
    assert spark.statements == ["SELECT 1"]
    assert spark.conf["fs.s3a.bucket.ducktest.aws.credentials.provider"] == env_provider()


def test_a_python_bodys_shell_gets_the_credentials_sdk_env(tmp_path, remote_inputs):
    given = []

    def factory(binary, *, home=None, env=None):
        given.append(env)
        return FakeShell(binary, home=home, env=env)

    def body(ctx):
        ctx.client("duck").oks("SELECT 1")

    code, _, _, _ = go(tmp_path, [with_remote(py_test(body), remote_inputs)], shell_factory=factory)
    assert code == 0
    ((env,),) = [given]
    assert (env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"]) == ("admin", "password")


def test_a_body_reads_the_credentials_it_holds_with_their_values_masked(tmp_path, remote_inputs, monkeypatch):
    seen = {}

    def body(ctx):
        seen["one"] = ctx.credential("remote-key")
        seen["all"] = ctx.credentials()
        with pytest.raises(UsageError, match=r"ctx.credential\('nope'\): this test holds credentials \['remote-key'\]"):
            ctx.credential("nope")

    code, _, _, _ = go(tmp_path, [with_remote(py_test(body), remote_inputs)])
    assert code == 0
    view = seen["one"]
    assert seen["all"] == [view]
    assert (view.name, view.type, view.secret_name, view.storages, view.scope) == (
        "remote-key",
        "s3",
        "ducktest_remote_key",
        ("remote",),
        ["s3://ducktest/delta"],
    )
    assert view.env == ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    assert view.fields == {"key_id": "***", "secret": "***"}
    assert view.source == "env DT_KEY, DT_SECRET"


def test_a_failed_test_says_what_its_engines_did_not_take_from_the_shells_env(tmp_path, remote_inputs, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "mine")

    def body(ctx):
        raise AssertionError("no")

    _, _, report, _ = go(tmp_path, [with_remote(py_test(body), remote_inputs)])
    made_of = report["tests"][0]["made_of"]["environment"]
    assert {
        "what": "process env removed",
        "is": "AWS_PROFILE",
        "from": "the shell's env (AWS_ of S3CredentialDef)",
    } in made_of


def test_a_credentials_env_counts_toward_the_engines_identity_by_source_never_by_value(monkeypatch):
    project = LoadedProject("/p", {}, {}, {}, {})
    storage = StorageDef("bucket", "s3://bucket")

    def identity(keys):
        credential = S3CredentialDef("key", sources=[from_env(keys)], scopes=["bucket"])
        test = Test(TestDef(path="t.py", name="t", type=PYTHON), {}, ((storage, RW), (credential, RO)))
        (engine,) = leases.bind(test, resolve(test), project)
        return planned_identity(engine)

    monkeypatch.setenv("DT_KEY", "one")
    monkeypatch.setenv("DT_SECRET", "s")
    monkeypatch.setenv("DT_OTHER", "o")
    first = identity({"DT_KEY": "key_id", "DT_SECRET": "secret"})
    monkeypatch.setenv("DT_KEY", "two")
    assert identity({"DT_KEY": "key_id", "DT_SECRET": "secret"}) == first
    assert identity({"DT_OTHER": "key_id", "DT_SECRET": "secret"}) != first


def test_a_booted_engine_boots_with_its_tests_credentials_and_no_sdk_name_of_the_shell(
    tmp_path, remote_inputs, monkeypatch
):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "mine")
    spark, at_boot = SparkRecorder(), {}
    booted = spark._boot

    def boot(log_path):
        at_boot.update({n: v for n, v in os.environ.items() if n.startswith("AWS_")})
        return booted(log_path)

    spark._boot = boot
    test = with_remote(py_test(passes, engines=["spark"]), remote_inputs)
    code, _, _, _ = go(tmp_path, [test], engines=[spark.engine()])
    assert code == 0
    assert at_boot == {"AWS_ACCESS_KEY_ID": "admin", "AWS_SECRET_ACCESS_KEY": "password"}
    assert "AWS_ACCESS_KEY_ID" not in os.environ and os.environ["AWS_PROFILE"] == "mine", "the run's own env is back"


def test_a_worker_engine_is_reset_between_tests_and_reopened_after_a_failure(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        made.append(FakeShell(binary, home=home, env=env))
        made[-1].calls = []
        made[-1].begin_test = lambda s=made[-1]: s.calls.append("reset")
        made[-1].close = lambda s=made[-1]: s.calls.append("close")
        return made[-1]

    def fails(ctx):
        raise AssertionError("no")

    tests = [py_test(passes, path="test/a.py"), py_test(fails, path="test/b.py"), py_test(passes, path="test/c.py")]
    go(tmp_path, tests, shell_factory=factory, workers=1)
    first, second = made  # the second shell is closed when the run ends
    assert (first.calls, second.calls) == (["reset", "reset", "close"], ["reset", "close"])


def test_a_batch_scoped_engine_is_closed_when_its_python_test_ends(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        made.append(FakeShell(binary, home=home, env=env))
        made[-1].closed = 0
        made[-1].close = lambda s=made[-1]: setattr(s, "closed", s.closed + 1)
        return made[-1]

    def body(ctx):
        ctx.client("own").oks("SELECT 1")

    tests = [py_test(body, path=f"test/{n}.py", engines=["own"]) for n in "ab"]
    code, _, _, _ = go(
        tmp_path, tests, shell_factory=factory, workers=1, engines=[DuckEngineDef("own", lifetime="batch")]
    )
    assert code == 0
    assert [s.closed >= 1 for s in made] == [True, True]


def test_an_engine_that_cannot_open_is_a_setup_error_of_the_test(tmp_path):
    def body(ctx):
        pass

    engines = [DuckEngineDef("bad", requires=["nope"])]
    factory = lambda b, **kw: FakeShell(preamble_fails=True)  # noqa: E731
    _, _, report, _ = go(tmp_path, [py_test(body, engines=["bad"])], shell_factory=factory, engines=engines)
    assert report["tests"][0]["outcome"] == "error"
    assert "could not be set up: SqlError: Catalog Error" in report["tests"][0]["problems"][0]


def test_an_engine_of_lifetime_run_is_one_instance_for_the_run(tmp_path):
    clones = []

    class Cloning(FakeEngine):
        def for_worker(self):
            clones.append(1)
            return self

    engine = Cloning()
    tests = [py_test(passes, path=f"test/{n}.py", engines=["fake"]) for n in "abcd"]
    code, _, _, _ = go(tmp_path, tests, engines=[engine.engine().with_(lifetime="run")], workers=4)
    assert (code, len(clones), engine.calls.count("begin_test")) == (0, 1, 4)


# -----------------------------------------------------------------------------
# The Context of a test
#


def test_a_value_set_on_the_context_reaches_every_client_and_one_set_on_a_client_only_that_one(tmp_path):
    fake = FakeEngine()
    ctx = ctx_for(py_test(passes, engines=["fake"]), tmp_path, engine=fake)
    ctx.values.set(path="/p")
    ctx.client("fake").values.set(only="x")
    ctx.client("fake").oks("SELECT '{path}', '{only}'")
    ctx.client("duck").oks("SELECT '{path}'")
    with pytest.raises(UsageError, match="no value for {only}"):
        ctx.client("duck").oks("SELECT '{only}'")
    assert fake.statements == ["SELECT '/p', 'x'"]
    assert ctx.client("duck").engine.live.statements == ["SELECT '/p'"]
    assert (ctx.values.path, ctx.client("fake").values.path, "only" in ctx.values) == ("/p", "/p", False)


def test_a_client_template_sees_the_names_a_test_file_sees_and_no_secret(tmp_path, remote_inputs):
    seen = {}

    def body(ctx):
        seen.update(names=ctx.values, location=ctx.location("t"))
        ctx.client("duck").oks("SELECT '{TEMP_DIR}', '{CATALOG}', '{catalog}'")

    storage = StorageDef("local", str(tmp_path / "local-root"))
    local = py_test(body, values={"catalog": "c"}, data=StorageDef("fixtures", "/data"), temp=storage)
    code, _, report, _ = go(tmp_path / "local", [local])
    assert code == 0, report["tests"][0].get("failure")
    names = seen["names"]
    assert {n: names[n] for n in ("DATA_DIR", "LOCAL_DATA_DIR", "CATALOG", "catalog")} == {
        "DATA_DIR": "/data",
        "LOCAL_DATA_DIR": "/data",
        "CATALOG": "c",
        "catalog": "c",
    }
    assert names["LOCAL_TEMP_DIR"] == names["TEMP_DIR"] == os.path.dirname(seen["location"])

    seen.clear()
    remote = with_remote(py_test(body, values={"catalog": "c"}), remote_inputs)
    assert go(tmp_path / "remote", [remote])[0] == 0
    names = seen["names"]
    assert names["TEMP_DIR"].startswith("s3://ducktest/delta/temp/")
    assert "LOCAL_TEMP_DIR" not in names and "AWS_SECRET_ACCESS_KEY" not in names


def test_a_python_body_s_own_place_under_a_local_storage_exists_when_it_begins(tmp_path):
    seen = {}

    def body(ctx):
        seen["exists"] = os.path.isdir(ctx.values.temp_dir)

    storage = StorageDef("scratch", str(tmp_path / "scratch"))
    assert go(tmp_path, [py_test(body, temp=storage)])[0] == 0
    assert seen == {"exists": True}


def test_names_are_case_insensitive_and_two_spellings_of_one_are_an_error(tmp_path):
    fake = FakeEngine()
    ctx = ctx_for(py_test(passes, engines=["fake"], values={"Catalog": "c"}), tmp_path, engine=fake)
    ctx.values.set(path="/p")
    ctx.client("fake").oks("SELECT '{PATH}', '{path}', '{catalog}', '{CATALOG}'")
    ctx.client("fake").oks("SELECT '{PATH}'", PATH="/own")
    assert fake.statements == ["SELECT '/p', '/p', 'c', 'c'", "SELECT '/own'"]
    assert ctx.values["catalog"] == ctx.values["CATALOG"] == ctx.values.Catalog == "c"
    with pytest.raises(UsageError, match="'path' and 'PATH' are one name"):
        ctx.client("fake").oks("SELECT 1", path="a", PATH="b")
    with pytest.raises(UsageError, match="are one name"):
        ctx.values.set(path="a", PATH="b")


def test_a_later_tier_replaces_a_name_in_any_case():
    definition = TestDef(path="t.py", name="t", type=PYTHON)
    definition.apply(Options.of({"values": {"path": "rule"}}, "rule"), "ducktest_conf.py: default")
    definition.apply(Options.of({"values": {"PATH": "own"}}, "@test"), "@test")
    env = resolve(Test(definition))
    assert (env.value_map, env.names) == ({"PATH": "own"}, {"PATH": "own"})


def test_a_body_reads_each_parameter_of_its_cell_by_name_and_a_local_values_fields_are_template_values(tmp_path):
    fake = FakeEngine()
    test = py_test(passes, {"storage": matrix_value("s3")}, engines=["fake"])
    test.cell["case"] = Value("case", "wide", data={"width": 9})
    ctx = ctx_for(test, tmp_path, engine=fake)
    assert (ctx.params.storage, ctx.params["case"], ctx.params.case["width"]) == ("s3", {"width": 9}, 9)
    with pytest.raises(AttributeError, match=r"no parameter 'cse'; this cell has \['case', 'storage'\]"):
        ctx.params.cse
    ctx.client("fake").oks("SELECT {width}, '{storage}'")
    assert fake.statements == ["SELECT 9, 's3'"]
    assert test.name == "test/body.py::passes[storage=s3,case=wide]"


def test_a_context_holds_one_client_per_engine_and_its_identity_is_theirs_and_the_environment(tmp_path):
    ctx = ctx_for(py_test(passes, engines=["fake"]), tmp_path)
    assert ctx.client("fake") is ctx.client("fake") is ctx.clients["fake"]
    other = ctx_for(py_test(passes), tmp_path)
    assert ctx.identity != other.identity
    assert other.identity == ctx_for(py_test(passes), tmp_path).identity


# -----------------------------------------------------------------------------
# Declared in ducktest_conf.py
#


def test_engines_are_declared_among_the_engines_and_named_by_a_test(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest import Project\nfrom ducktest.engines.duckdb import DuckEngineDef\nproject = Project(engines=[DuckEngineDef('duck', requires=['delta'])])\n"
    )
    (tmp_path / "t.py").write_text(
        "from ducktest import test\n\n@test(engines=['duck'])\ndef a(ctx):\n    pass\n\n"
        "@test(engines=['nope'])\ndef b(ctx):\n    pass\n"
    )
    with pytest.raises(UsageError, match=r"t.py:7: engine='nope' is not declared; the declared ones are 'duck'"):
        scan(str(tmp_path), working_dir=str(tmp_path))


# -----------------------------------------------------------------------------
# A .test batch is one identity
#

ENGINES_CONF = """
from ducktest import DuckEngineDef, Project, match

project = Project(
    engines=[
        DuckEngineDef("delta", requires=["delta"]),
        DuckEngineDef("json", requires=["json"]),
        DuckEngineDef("also_json", requires=["json"]),
    ],
    rules=[
        match("delta.test", engines=["delta"]),
        match("json.test", engines=["json"]),
        match("also_json.test", engines=["also_json"]),
        match("inline_require.test", engines=["json"]),
    ],
)
"""


@pytest.fixture
def bound(tmp_path, monkeypatch):
    """A project whose `.test` files a rule each binds to an engine."""
    (tmp_path / "ducktest_conf.py").write_text(ENGINES_CONF)
    for name in ("delta", "json", "also_json"):
        (tmp_path / f"{name}.test").write_text("query I\nSELECT 1\n----\n1\n")
    (tmp_path / "inline_require.test").write_text("require json\n\nquery I\nSELECT 1\n----\n1\n")
    (tmp_path / "body.py").write_text(
        "from ducktest import test\n\n@test(engines=['json'])\ndef body(ctx):\n    pass\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def batch_of_each_file(tmp_path):
    tests, project = scan(str(tmp_path), working_dir=str(tmp_path))
    plan = phases.build_plan([c for c in tests if c.definition.type == SQLLOGIC], project)
    return {f: b["name"] for b in plan["batches"] for f in b["files"]}


def test_test_files_share_a_batch_exactly_when_their_engines_have_one_identity(bound):
    batch = batch_of_each_file(bound)
    assert batch["delta.test"] != batch["json.test"]
    # two names for one content are one engine; an inline `require` is part of the body, not of it
    assert batch["json.test"] == batch["also_json.test"] == batch["inline_require.test"]


def test_a_test_file_s_identity_is_that_of_the_duck_sqllogic_engine_of_its_batch(bound):
    tests, project = scan(str(bound), working_dir=str(bound))
    of = {t.definition.path: leases.environment(t, resolve(t), project) for t in tests if t.definition.type == SQLLOGIC}
    unittest = UnittestEngineDef("duck-sqllogic", environment=of["json.test"].key()).make()
    assert (unittest.dialect, unittest.definition.lifetime) == ("duck-sqllogic", "batch")
    assert leases.batch_identity(of["json.test"]) == leases.identity([unittest.identity], of["json.test"])
    assert leases.batch_identity(of["json.test"]) == leases.batch_identity(of["also_json.test"])
    assert leases.batch_identity(of["json.test"]) != leases.batch_identity(of["delta.test"])


def test_a_plan_labels_each_batch_by_the_identity_of_its_tests(bound):
    tests, project = scan(str(bound), working_dir=str(bound))
    by_file = {f: b["key"]["digest"] for b in phases.build_plan(tests, project)["batches"] for f in b["files"]}
    (json_test,) = [t for t in tests if t.definition.path == "json.test"]
    batch = leases.batch_identity(leases.environment(json_test, resolve(json_test), project))
    assert by_file["json.test"] == batch[:12]
    # a Python body leases a DuckDB engine, not a batch's unittest one: never the label of a .test batch
    assert by_file["body.py"] not in {by_file["json.test"], by_file["delta.test"]}


def test_scan_env_shows_the_clients_of_each_test_and_what_their_engines_render(bound, capsys):
    assert main(["scan", "--env"]) == 0
    printed = capsys.readouterr().out
    assert (
        "delta.test::delta\n  engine: delta, as duck-sqllogic  <- engines delta\n    require: delta  <- engine delta\n"
        in printed
    )
    assert "body.py::body\n  client: json, as duck-shell  <- engines json\n    require: json  <- engine json\n" in printed


def test_the_engine_comes_first_in_the_init_file_and_nothing_is_given_twice():
    duck = DuckEngineDef("duck", requires=["httpfs", "json"], settings={"threads": 2, "memory_limit": "1GB"})
    project = LoadedProject("/p", {"duck": duck}, {}, {}, {})
    test = sql_test(
        "a.test", {"storage": matrix_value("s3", requires=["httpfs"])}, engines=["duck"], settings={"threads": 4}
    )
    env = leases.environment(test, resolve(test), project)
    text, _ = init_file_text(env)
    assert text == (
        "require json\n\nrequire httpfs\n\n"
        "statement ok\nSET memory_limit = '1GB';\n\n"
        "statement ok\nSET threads = '4';\n"
    )


def test_the_batch_of_a_test_file_is_given_its_engine(tmp_path):
    (tmp_path / "a.test").write_text("query I\nSELECT 1\n----\n1\n")
    test = sql_test("a.test", engines=["duck"])
    _, run_dir, _, _ = go(tmp_path, [test], engines=[DuckEngineDef("duck", requires=["delta"])], keep=True)
    (init,) = run_dir.glob("batches/*/init.sqllogic")
    assert init.read_text() == "require delta\n"


def test_a_test_file_runs_on_one_engine(tmp_path):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest import Project, default\nfrom ducktest.engines.duckdb import DuckEngineDef\n"
        "project = Project(engines=[DuckEngineDef('a'), DuckEngineDef('b')], rules=[default(engines=['a', 'b'])])\n"
    )
    (tmp_path / "x.test").write_text("query I\nSELECT 1\n----\n1\n")
    with pytest.raises(UsageError, match=r"names engines \['a', 'b'\]; a .test body runs on one engine"):
        scan(str(tmp_path), working_dir=str(tmp_path))


# -----------------------------------------------------------------------------
# The DuckDB engine of a test that names none
#


def engines_project(root, engines, rules="[]"):
    root.mkdir()
    (root / "ducktest_conf.py").write_text(
        "from ducktest import Project, default\nfrom ducktest.engines.duckdb import DuckEngineDef\n"
        f"project = Project(engines=[{engines}], rules={rules})\n"
    )
    (root / "x.test").write_text("query I\nSELECT 1\n----\n1\n")
    (root / "body.py").write_text("from ducktest import test\n\n@test()\ndef body(ctx):\n    pass\n")
    tests, project = scan(str(root), working_dir=str(root))
    return {t.definition.name: t for t in tests}, project


def default_roots(root):
    return (
        f"  root: temp_dir_root={root}/ducktest_temp/<run_token>  <- default storage\n"
        f"  root: data_dir={root}/data  <- default storage\n"
    )


def test_the_projects_only_duckdb_engine_is_the_default(tmp_path, monkeypatch, capsys):
    tests, project = engines_project(tmp_path / "p", "DuckEngineDef('duck', requires=['json'])")
    assert [d.name for d in leases.bind(tests["body"], resolve(tests["body"]), project)] == ["duck"]
    assert leases.environment(tests["x"], resolve(tests["x"]), project).extensions == ["json"]
    monkeypatch.chdir(tmp_path / "p")
    assert main(["scan", "--env"]) == 0
    roots = default_roots(tmp_path / "p")
    assert capsys.readouterr().out == (
        "body.py::body\n"
        "  client: duck, as duck-shell  <- engines duck (the only DuckDB engine)\n"
        "    require: json  <- engine duck\n"
        f"{roots}"
        "x.test::x\n"
        "  engine: duck, as duck-sqllogic  <- engines duck (the only DuckDB engine)\n"
        "    require: json  <- engine duck\n"
        f"{roots}"
    )


def test_with_several_duckdb_engines_a_test_names_one(tmp_path):
    with pytest.raises(UsageError, match=re.escape("names no DuckDB engine, and the project declares ['a', 'b']")):
        engines_project(tmp_path / "unnamed", "DuckEngineDef('a'), DuckEngineDef('b')")
    tests, project = engines_project(
        tmp_path / "named", "DuckEngineDef('a'), DuckEngineDef('b')", "[default(engines=['b'])]"
    )
    assert {name: leases.duckdb_of(t, project).name for name, t in tests.items()} == {"body": "b", "x": "b"}


def test_with_no_duckdb_engine_a_test_runs_on_the_built_in_one(tmp_path, monkeypatch, capsys):
    tests, project = engines_project(tmp_path / "p", "")
    assert [leases.duckdb_of(t, project) for t in tests.values()] == [leases.DEFAULT, leases.DEFAULT]
    monkeypatch.chdir(tmp_path / "p")
    assert main(["scan", "--env"]) == 0
    roots = default_roots(tmp_path / "p")
    assert capsys.readouterr().out == f"body.py::body\n{roots}x.test::x\n{roots}"
