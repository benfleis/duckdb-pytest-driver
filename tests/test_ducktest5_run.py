"""ducktest5's run loop, with no DuckDB and no Spark: a fake engine, a fake service, a fake DuckDB
session and the fake unittest binary."""

import io
import json
import os
import re
import sys

import pytest
import yaml

from ducktest5.binary import UsageError
from ducktest5.client import SqlError
from ducktest5.conf import Project
from ducktest5.ctx import Ctx
from ducktest5.declare import EngineDef, ServiceDef, Variant
from ducktest5.events import TEST_END
from ducktest5.events import read as read_events
from ducktest5.model import PAIRED, PYTHON, SQLLOGIC, Test, TestDef
from ducktest5.provision import EXISTING, Provisioned
from ducktest5.run import _run_body, run
from ducktest5.testenv import resolve

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")
FAKE_DEBUGGER = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_debugger.py")


class FakeEngine:
    """An engine that answers every statement with one row, and records what it was asked."""

    def __init__(self, *, boots=True):
        self.boots, self.calls, self.statements = boots, [], []

    def engine(self, id="fake", uses=()):
        return EngineDef(id, "fake:1", self._boot, uses=uses)

    def _boot(self, log_path):
        self.calls.append("boot")
        if not self.boots:
            raise RuntimeError("no JVM")
        return self

    def use_service(self, service_id, block):
        self.calls.append(f"use {service_id}")

    def begin_test(self):
        self.calls.append("begin_test")

    def run(self, statements):
        self.statements += statements
        if any("boom" in s for s in statements):
            raise SqlError("boom", statement=len(statements) - 1)
        return ["v"], [(1,)]

    def stop(self):
        self.calls.append("stop")


class FakeStore:
    """A service that is found, never started. `up` is whether it answers."""

    def __init__(self, *, up=True, clean_raises=False):
        self.up, self.clean_raises, self.cleaned, self.calls = up, clean_raises, [], []

    def service(self, id="store"):
        def clean(block, uri):
            if self.clean_raises:
                raise RuntimeError("access denied")
            self.cleaned.append(uri)

        return ServiceDef(
            id,
            block=lambda **o: {"endpoint": "http://store", "secret_key": "s3cr3t"},
            alive=lambda block: self.up,
            setup=lambda block: self.calls.append("setup"),
            clean=clean,
            location=lambda block, *parts: "fake://" + "/".join(parts),
            secrets=("secret_key",),
        )


class FakeSession:
    """Stands in for the run's DuckDB shell: keeps the text it was given."""

    def __init__(self, binary=None, *, home=None, env=None, preamble_fails=False):
        self.preambles, self.statements, self.preamble_fails, self.home = [], [], preamble_fails, home
        self.env, self.timeouts = env, []

    def begin_test(self):
        self.preambles = []

    def add_preamble(self, preamble):
        if self.preamble_fails:
            raise SqlError("Catalog Error: no such secret type")
        self.preambles.append(preamble)

    def run(self, statements, *, timeout=None):
        self.statements += statements
        self.timeouts.append(timeout)
        return ["v"], [(1,)]

    restart = respawn = close = lambda self: None


def sql_test(path, axis_values=None, **fields):
    name = os.path.splitext(os.path.basename(path))[0]
    return Test(TestDef(path=path, name=name, type=SQLLOGIC, body_file=path, **fields), None, axis_values or {})


def py_test(body, axis_values=None, *, path="test/body.py", body_file=None, **fields):
    definition = TestDef(
        path=path, name=body.__name__, type=PAIRED if body_file else PYTHON, body=body, body_file=body_file, **fields
    )
    return Test(definition, None, axis_values or {})


def go(tmp_path, tests, *, engines=(), services=(), unittest_bin=FAKE, duckdb_bin="no-duckdb", **kwargs):
    """Returns (exit code, run dir, report, printed text)."""
    project = Project(str(tmp_path), {e.id: e for e in engines}, {s.id: s for s in services}, {}, {}, ())
    out = io.StringIO()
    kwargs.setdefault("session_factory", FakeSession)
    code = run(
        tests,
        project,
        duckdb_bin=duckdb_bin,
        unittest_bin=unittest_bin,
        working_dir=str(tmp_path),
        temp_root=str(tmp_path / "runs"),
        out=out,
        **kwargs,
    )
    (run_dir,) = (tmp_path / "runs").iterdir()
    report = yaml.safe_load((run_dir / "05-report.yaml").read_text())
    return code, run_dir, report, out.getvalue()


def outcomes(report):
    return {c["id"].split("::")[-1]: c["outcome"] for c in report["tests"]}


def result_of(report, name):
    """One test of the report by its name. Tests are listed in the order they ended, which with
    several workers is not the order they were given in."""
    return next(c for c in report["tests"] if c["id"].split("::")[-1] == name)


def end_events(run_dir):
    """The `test_end` lines of a run: one per test, whatever else the stream holds."""
    return [e for e in read_events(str(run_dir)) if e["event"] == TEST_END]


@pytest.fixture
def flaky_state(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("FAKE_UNITTEST_STATE", str(tmp_path / "state"))


def passes(ctx):
    pass


def fails(ctx):
    assert 1 == 2


# -----------------------------------------------------------------------------
# Exit codes
#


@pytest.mark.parametrize(
    "names, code",
    [
        (["a.test"], 0),
        (["a.test", "b_fail.test"], 1),
        (["a.test", "b_crash.test"], 1),
        (["a.test", "c_skip.test"], 0),  # a skip inside the binary does not fail a run
        (["c_skip.test"], 1),  # but a run in which nothing passed is not green
    ],
)
def test_the_exit_code_says_whether_everything_that_ran_passed(tmp_path, names, code):
    assert go(tmp_path, [sql_test(n) for n in names])[0] == code


def test_a_blocked_test_fails_the_run(tmp_path):
    store = FakeStore(up=False)
    code, _, report, _ = go(
        tmp_path, [py_test(passes), py_test(passes, services=["store"], path="test/s.py")], services=[store.service()]
    )
    assert code == 1
    assert sorted(c["outcome"] for c in report["tests"]) == ["blocked", "passed"]


# -----------------------------------------------------------------------------
# Batches
#


def batch_of(report):
    return {c["id"].split("::")[-1]: c["batch"] for c in report["tests"]}


def test_tests_of_one_file_never_share_a_batch(tmp_path):
    axis = {"a": Variant("a", tags=("x",)), "b": Variant("b", tags=("y",))}
    tests = [
        sql_test("t.test", {"k": axis["a"]}),
        sql_test("t.test", {"k": axis["b"]}),
        sql_test("u.test", {"k": axis["a"]}),
    ]
    code, run_dir, report, _ = go(tmp_path, tests, keep=True)
    assert code == 0
    batches = batch_of(report)
    assert batches["t[k=a]"] != batches["t[k=b]"]
    assert batches["u[k=a]"] == batches["t[k=a]"]  # the same environment: it rides along
    assert (run_dir / batches["t[k=a]"] / "tests.txt").read_text() == "t.test\nu.test\n"
    assert (run_dir / batches["t[k=b]"] / "tests.txt").read_text() == "t.test\n"


def test_tests_with_other_values_or_settings_go_in_other_batches(tmp_path):
    tests = [
        sql_test("a.test"),
        sql_test("b.test"),
        sql_test("c_env.test", values={"catalog": "duck"}),
        sql_test("d.test", settings={"threads": 2}),
    ]
    _, run_dir, report, _ = go(tmp_path, tests, keep=True)
    batches = batch_of(report)
    assert batches["a"] == batches["b"]
    assert len({batches["a"], batches["c_env"], batches["d"]}) == 3
    config = json.loads((run_dir / batches["c_env"] / "config.json").read_text())
    assert config["test_env"] == [{"env_name": "CATALOG", "env_value": "duck"}]
    # a setting is delivered as SET in the init file, never as the binary's own `settings`
    config = json.loads((run_dir / batches["d"] / "config.json").read_text())
    assert "settings" not in config
    assert (run_dir / batches["d"] / "init.sqllogic").read_text() == "statement ok\nSET threads = '2';\n"


def test_a_batch_holds_at_most_batch_size_files_and_a_slow_file_runs_alone(tmp_path):
    tests = [sql_test(f"t{n}.test") for n in range(3)] + [sql_test("s.test_slow", tags=["slow"])]
    _, _, report, _ = go(tmp_path, tests, batch_size=2)
    batches = batch_of(report)
    assert sorted(list(batches.values()).count(b) for b in set(batches.values())) == [1, 1, 2]
    assert list(batches.values()).count(batches["s"]) == 1


def test_a_test_with_a_service_runs_alone_and_its_failure_is_not_run_a_second_time(tmp_path, flaky_state):
    store = FakeStore()
    tests = [sql_test("a.test"), sql_test("b.test", services=["store"]), sql_test("c_flaky2.test", services=["store"])]
    _, _, report, _ = go(tmp_path, tests, services=[store.service()])
    assert len(set(batch_of(report).values())) == 3
    assert outcomes(report)["c_flaky2"] == "failed"
    assert (tmp_path / "state" / "c_flaky2.test.runs").read_text() == "1"


def test_a_variant_init_sql_is_the_batch_init_file(tmp_path):
    store = FakeStore()
    variant = Variant("s3", services=("store",), init_sql=lambda blocks: f"ATTACH '{blocks['store']['endpoint']}'")
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test", {"k": variant})], services=[store.service()], keep=True)
    init = run_dir / report["tests"][0]["batch"] / "init.sqllogic"
    assert init.read_text() == "statement ok\nATTACH 'http://store';\n"


def test_requires_settings_and_init_sql_reach_the_init_file_in_that_order(tmp_path):
    variant = Variant(
        "curl",
        requires=("httpfs", "json"),
        settings={"httpfs_client_implementation": "curl"},
        init_sql="CREATE SECRET s (TYPE S3)",
    )
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test", {"client": variant})], keep=True)
    init = run_dir / report["tests"][0]["batch"] / "init.sqllogic"
    assert init.read_text() == (
        "require httpfs\n\nrequire json\n\n"
        "statement ok\nSET httpfs_client_implementation = 'curl';\n\n"
        "statement ok\nCREATE SECRET s (TYPE S3);\n"
    )


def test_a_variant_gives_the_binary_its_roots(tmp_path):
    variant = Variant("http", roots={"temp_dir_root": "http://origin/temp", "local_temp_dir_root": "/www/temp"})
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test", {"storage": variant})], keep=True)
    config = json.loads((run_dir / report["tests"][0]["batch"] / "config.json").read_text())
    assert config["temp_dir_root"] == "http://origin/temp"
    assert config["local_temp_dir_root"] == "/www/temp"


def test_a_root_the_binary_does_not_have_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError, match="root 'tmp' is not one of"):
        go(tmp_path, [sql_test("a.test", {"storage": Variant("http", roots={"tmp": "/x"})})])


def test_a_variant_sets_and_unsets_variables_in_the_environment_of_the_unittest_process(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "LEAKED-FROM-THE-CALLER")
    variant = Variant("env", env={"AWS_PROFILE": "ducktest"}, env_unset=("AWS_ACCESS_KEY_ID",))
    _, run_dir, report, _ = go(tmp_path, [sql_test("a_showenv.test", {"auth": variant})], keep=True)
    output = (run_dir / report["tests"][0]["batch"] / "output.txt").read_text()
    printed = json.loads(output.split("PROCESS_ENV ")[1].splitlines()[0])
    assert printed == {"AWS_PROFILE": "ducktest", "AWS_ACCESS_KEY_ID": None}


def test_tests_with_other_environments_never_share_a_unittest_process(tmp_path):
    axis = {
        "plain": Variant("plain"),
        "loaded": Variant("loaded", requires=("httpfs",)),
        "rooted": Variant("rooted", roots={"data_dir": "/d"}),
        "noisy": Variant("noisy", env={"AWS_PROFILE": "p"}),
    }
    tests = [sql_test(f"t{n}.test", {"k": v}) for n, v in enumerate(axis.values())]
    _, _, report, _ = go(tmp_path, tests + [sql_test("t9.test", {"k": axis["plain"]})], keep=True)
    batches = batch_of(report)
    assert len(set(batches.values())) == 4  # one for each environment; the two plain ones ride along
    assert batches["t0[k=plain]"] == batches["t9[k=plain]"]


SECRET = Variant("s3", init_sql="CREATE SECRET (SECRET 'hunter2')")


def files_with_the_secret(run_dir):
    return [p.name for p in run_dir.rglob("*") if p.is_file() and "hunter2" in p.read_text(errors="replace")]


@pytest.mark.parametrize("keep", [False, True])
def test_init_sql_does_not_stay_in_a_batch_that_is_kept_for_its_failure(tmp_path, keep):
    tests = [sql_test("a.test", {"k": SECRET}), sql_test("b_fail.test", {"k": SECRET})]
    _, run_dir, _, _ = go(tmp_path, tests, keep=keep)
    inits = sorted(str(p.relative_to(run_dir / "batches")) for p in run_dir.rglob("init.sqllogic"))
    assert inits == ["b001/alone-b_fail.test/init.sqllogic", "b001/init.sqllogic"]
    # with --keep the user asked for everything, and that is how the init SQL is got back
    assert files_with_the_secret(run_dir) == (["init.sqllogic"] * 2 if keep else [])
    if not keep:
        assert "again with --keep" in (run_dir / "batches" / "b001" / "init.sqllogic").read_text()


def test_init_sql_does_not_stay_in_the_directory_of_a_failed_paired_test(tmp_path):
    test = py_test(runs_its_file, {"k": SECRET}, body_file="test/p_fail.test")
    _, run_dir, report, _ = go(tmp_path, [test])
    assert (run_dir / "tests" / test.dir_name / "run_test" / "init.sqllogic").exists()
    assert files_with_the_secret(run_dir) == []


# -----------------------------------------------------------------------------
# --lldb: the batch as usual, then the debugger instead of the run
#


@pytest.fixture
def debugger(tmp_path, monkeypatch):
    """The fake debugger, and what it recorded once it has been started."""
    record = tmp_path / "debugger.json"
    monkeypatch.setenv("FAKE_DEBUGGER_RECORD", str(record))

    def started():
        return json.loads(record.read_text())

    return f"{sys.executable} {FAKE_DEBUGGER} --", started


def debug(tmp_path, tests, command, *, services=(), **kwargs):
    project = Project(str(tmp_path), {}, {s.id: s for s in services}, {}, {}, ())
    out = io.StringIO()
    code = run(
        tests,
        project,
        duckdb_bin="no-duckdb",
        unittest_bin=FAKE,
        working_dir=str(tmp_path),
        temp_root=str(tmp_path / "runs"),
        out=out,
        session_factory=FakeSession,
        debugger=command,
        **kwargs,
    )
    return code, out.getvalue()


def test_the_debugger_gets_the_binary_and_the_batch_a_normal_run_would_have_used(tmp_path, debugger):
    command, started = debugger
    test = sql_test("a.test", {"storage": MADE_OF}, values={"catalog": "duck"})
    code, text = debug(tmp_path, [test], command, services=[FakeStore().service()])
    argv = started()["argv"]
    batch_dir = os.path.dirname(argv[argv.index("-f") + 1])
    assert code == 0
    assert argv == ["--", FAKE, "--test-dir", str(tmp_path), "-f", f"{batch_dir}/tests.txt"] + [
        "--test-config",
        f"{batch_dir}/config.json",
        "--emit-test-events",
        "--emit-on-skip",
    ]
    assert started()["cwd"] == str(tmp_path)
    assert open(f"{batch_dir}/tests.txt").read() == "a.test\n"
    config = json.loads(open(f"{batch_dir}/config.json").read())
    assert config["test_env"] == [
        {"env_name": "CATALOG", "env_value": "duck"},
        {"env_name": "ROOT", "env_value": "s3://bucket"},
        {"env_name": "STORAGE", "env_value": "s3"},
    ]
    assert config["init_sqllogic"] == f"{batch_dir}/{'init.sqllogic'}"
    assert f"\nbatch kept: {batch_dir}\n" in text  # it stays, for a second look or a second debugger
    assert "may hold a secret" in text
    assert "passed" not in text  # nothing ran here, so there is nothing to report


def test_the_exit_code_of_the_debugger_is_the_exit_code_of_the_run(tmp_path, debugger, monkeypatch):
    monkeypatch.setenv("FAKE_DEBUGGER_EXIT", "3")
    assert debug(tmp_path, [sql_test("a.test")], debugger[0])[0] == 3


@pytest.mark.parametrize(
    "tests, message",
    [
        (
            [sql_test("a.test"), sql_test("b.test")],
            "exactly one test of a .test file; the selection gives 2:\n  a.test::a",
        ),
        ([], "exactly one test of a .test file; the selection gives 0"),
        ([py_test(passes)], "is a Python body -- not supported yet"),
    ],
)
def test_a_debugger_needs_one_test_and_says_what_was_selected(tmp_path, debugger, tests, message):
    with pytest.raises(UsageError, match=re.escape(message)):
        debug(tmp_path, tests, debugger[0])
    assert not (tmp_path / "runs").exists()  # refused before the run dir is made


# -----------------------------------------------------------------------------
# A failure says what the test was made of
#


MADE_OF = Variant(
    "s3",
    services=("store",),
    values={"root": "s3://bucket"},
    settings={"s3_region": "eu"},
    init_sql="CREATE SECRET (SECRET 'hunter2')",
)


def test_a_failed_test_says_what_it_was_made_of(tmp_path):
    test = sql_test("b_fail.test", {"storage": MADE_OF})
    _, run_dir, report, text = go(tmp_path, [test], services=[FakeStore().service()])
    facts = report["tests"][0]["made_of"]
    assert facts["environment"] == [
        {"what": "set", "is": "s3_region=eu", "from": "AXES storage=s3"},
        {"what": "init sql", "is": "", "from": "AXES storage=s3"},
        {"what": "values", "is": "storage=s3", "from": "AXES storage"},
        {"what": "values", "is": "root=s3://bucket", "from": "AXES storage=s3"},
        {"what": "service", "is": "store (existing)", "from": "AXES storage=s3"},
    ]
    assert {name: os.path.basename(p) for name, p in facts["files"].items()} == {
        "config": "config.json",
        "tests": "tests.txt",
        "init": "init.sqllogic",
        "output": "output.txt",
    }
    assert all(os.path.isfile(p) for p in facts["files"].values())
    block = text.split("--- " + test.id)[1]
    assert "\n  values: root=s3://bucket  <- AXES storage=s3\n" in block
    assert "\n  set: s3_region=eu  <- AXES storage=s3\n" in block
    assert "\n  init sql  <- AXES storage=s3\n" in block
    assert "\n  service: store (existing)  <- AXES storage=s3\n" in block
    assert f"\n  init: {facts['files']['init']}\n" in block
    assert "\n  again: ducktest5 run --id " in block
    assert "hunter2" not in text  # the init SQL is named by its source, never printed


def test_a_failure_says_what_the_environment_of_the_process_was(tmp_path):
    variant = Variant(
        "env",
        requires=("httpfs",),
        roots={"data_dir": "s3://bucket/data"},
        env={"AWS_PROFILE": "ducktest", "AWS_SECRET_ACCESS_KEY": "hunter2"},
        env_unset=("AZURE_CLIENT_SECRET",),
    )
    test = sql_test("b_fail.test", {"auth": variant})
    _, _, _, text = go(tmp_path, [test])
    block = text.split("--- " + test.id)[1]
    assert "\n  require: httpfs  <- AXES auth=env\n" in block
    assert "\n  root: data_dir=s3://bucket/data  <- AXES auth=env\n" in block
    assert "\n  env: AWS_PROFILE=ducktest  <- AXES auth=env\n" in block
    assert "\n  env: AWS_SECRET_ACCESS_KEY=***  <- AXES auth=env\n" in block  # the name says it is one
    assert "\n  env unset: AZURE_CLIENT_SECRET  <- AXES auth=env\n" in block
    assert "hunter2" not in text


def test_a_passed_test_says_nothing_of_the_kind(tmp_path):
    _, _, report, text = go(tmp_path, [sql_test("a.test", {"storage": MADE_OF})], services=[FakeStore().service()])
    assert "made_of" not in report["tests"][0]
    assert "values: " not in text and "config: " not in text


def test_a_failed_python_test_names_its_test_directory(tmp_path):
    test = py_test(writes_and_fails, services=["store"])
    _, run_dir, report, text = go(tmp_path, [test], services=[FakeStore().service()])
    facts = report["tests"][0]["made_of"]
    assert facts["files"] == {"dir": str(run_dir / "tests" / test.dir_name)}
    assert f"\n  dir: {facts['files']['dir']}\n" in text


def test_a_secret_of_a_service_never_reaches_what_the_test_was_made_of(tmp_path):
    test = sql_test("b_fail.test", values={"token": "s3cr3t"}, services=["store"])
    _, _, report, text = go(tmp_path, [test], services=[FakeStore().service()])
    assert {"what": "values", "is": "token=***", "from": "@test"} in report["tests"][0]["made_of"]["environment"]
    assert "s3cr3t" not in text


def test_the_event_line_of_a_failure_holds_the_same_facts(tmp_path):
    test = sql_test("b_fail.test", {"storage": MADE_OF})
    _, run_dir, report, _ = go(tmp_path, [test], services=[FakeStore().service()])
    (event,) = end_events(run_dir)
    assert event["made_of"] == report["tests"][0]["made_of"]


# -----------------------------------------------------------------------------
# A failure in the init SQL is named, and is not blamed on the body
#


def test_a_failure_in_the_init_sql_is_an_error_that_names_the_variant_and_the_file(tmp_path):
    test = sql_test("a_initfail.test", {"storage": Variant("s3", init_sql="ATTACH 'nowhere'")})
    code, run_dir, report, text = go(tmp_path, [test], keep=True)
    assert (code, report["tests"][0]["outcome"]) == (1, "error")
    first_line = report["tests"][0]["problems"][0].splitlines()[0]
    assert first_line == f"in the init SQL of AXES storage=s3 ({run_dir / 'batches' / 'b001' / 'init.sqllogic'}:1)"
    assert "a_initfail.test:1" not in first_line  # not the body file, where the reader would find nothing
    assert first_line in text


def test_the_failing_statement_of_the_init_sql_names_its_own_source(tmp_path):
    axis_values = {"backend": Variant("oss", init_sql="SET a = 1"), "storage": Variant("s3", init_sql="SET b = 2")}
    _, _, report, _ = go(tmp_path, [sql_test("a_initfail2.test", axis_values)], keep=True)
    assert "in the init SQL of AXES storage=s3 (" in report["tests"][0]["problems"][0]


def test_a_setting_that_fails_in_the_init_sql_names_the_rule_that_asked_for_it(tmp_path):
    test = sql_test("a_initfail.test", settings={"threads": 2})
    test.definition.sources = {("settings", "threads"): "ducktest_conf.py: default"}
    _, _, report, _ = go(tmp_path, [test])
    assert "in the init SQL of ducktest_conf.py: default (" in report["tests"][0]["problems"][0]


def test_a_failure_in_the_body_stays_a_failure_when_the_batch_has_init_sql(tmp_path):
    test = sql_test("b_fail.test", {"storage": Variant("s3", init_sql="ATTACH 'nowhere'")})
    _, _, report, _ = go(tmp_path, [test])
    assert report["tests"][0]["outcome"] == "failed"


# -----------------------------------------------------------------------------
# What does not come up blocks its tests, and only those
#


def test_a_service_that_is_down_blocks_its_tests_and_their_engine_is_not_booted(tmp_path):
    store, engine = FakeStore(up=False), FakeEngine()
    tests = [py_test(passes, services=["store"], engines=["fake"]), py_test(passes, path="test/free.py")]
    code, run_dir, report, text = go(tmp_path, tests, engines=[engine.engine()], services=[store.service()])
    assert [c["outcome"] for c in report["tests"]] == ["blocked", "passed"]
    assert report["tests"][0]["blocked_by"] == ["store"]
    assert "does not answer" in report["not_provisioned"]["store"]
    assert engine.calls == []
    assert "'store' did not come up: 1 tests blocked" in text


def test_a_service_whose_start_raises_blocks_only_tests_with_a_callable_variant(tmp_path):
    def start(block):
        raise RuntimeError("minio image unpullable: exit 125")

    broken = ServiceDef("minio", block=lambda **o: {"endpoint": "http://minio"}, alive=lambda block: False, start=start)
    variant = Variant(
        "s3",
        services=("minio",),
        values=lambda blocks: {"root": blocks["minio"]["endpoint"]},
        init_sql=lambda blocks: f"ATTACH '{blocks['minio']['endpoint']}'",
    )
    tests = [sql_test("a.test", {"storage": variant}), sql_test("b.test")]
    code, run_dir, report, text = go(tmp_path, tests, services=[broken])
    assert outcomes(report) == {"a[storage=s3]": "blocked", "b": "passed"}
    assert report["tests"][0]["blocked_by"] == ["minio"]
    made_of = report["tests"][0]["made_of"]["environment"]
    assert {"what": "values", "is": "<from service minio>", "from": "AXES storage=s3"} in made_of
    assert {"what": "service", "is": "minio (did not come up)", "from": "AXES storage=s3"} in made_of
    assert "'minio' did not come up: 1 tests blocked" in text
    assert code == 1


def test_an_engine_that_does_not_boot_blocks_its_tests(tmp_path):
    engine = FakeEngine(boots=False)
    tests = [py_test(passes, engines=["fake"]), sql_test("a.test")]
    _, _, report, _ = go(tmp_path, tests, engines=[engine.engine()])
    assert outcomes(report) == {"passes": "blocked", "a": "passed"}
    assert "no JVM" in report["not_provisioned"]["fake"]


def test_an_engine_is_told_about_the_services_it_uses_and_is_stopped(tmp_path):
    store, engine = FakeStore(), FakeEngine()
    test = py_test(passes, services=["store"], engines=["fake"])
    go(tmp_path, [test], engines=[engine.engine(uses=("store", "absent"))], services=[store.service()])
    assert engine.calls == ["boot", "use store", "begin_test", "stop"]


def test_secrets_of_a_service_are_not_in_the_provision_record(tmp_path):
    _, run_dir, _, _ = go(tmp_path, [py_test(passes, services=["store"])], services=[FakeStore().service()])
    record = yaml.safe_load((run_dir / "03-provision.yaml").read_text())["services"]
    assert record == {
        "store": {
            "mode": EXISTING,
            "detached": False,
            "owned": False,
            "block": {"endpoint": "http://store", "secret_key": "***"},
            "attach": {"endpoint": "http://store"},
        }
    }


# -----------------------------------------------------------------------------
# Keep and clean
#


def writes(ctx):
    open(os.path.join(ctx.temp_dir, "f"), "w").close()
    ctx.location("tbl", on="store")


def writes_and_fails(ctx):
    writes(ctx)
    assert False


@pytest.mark.parametrize("keep", [False, True])
def test_what_a_passed_test_wrote_is_removed_unless_kept(tmp_path, keep):
    store = FakeStore()
    code, run_dir, report, _ = go(
        tmp_path, [py_test(writes, services=["store"])], services=[store.service()], keep=keep
    )
    assert code == 0
    uri = f"fake://{run_dir.name}/{py_test(writes).dir_name}/tbl"
    if keep:
        assert store.cleaned == [] and len(list((run_dir / "tests").iterdir())) == 1
        assert report["tests"][0]["kept"][1:] == [uri]
    else:
        assert store.cleaned == [uri] and list((run_dir / "tests").iterdir()) == []
        assert "kept" not in report["tests"][0]


def test_what_a_failed_test_wrote_stays_and_is_listed(tmp_path):
    store = FakeStore()
    _, run_dir, report, text = go(tmp_path, [py_test(writes_and_fails, services=["store"])], services=[store.service()])
    kept = report["tests"][0]["kept"]
    assert store.cleaned == [] and os.path.isfile(os.path.join(kept[0], "f")) and kept[1].startswith("fake://")
    assert "kept: " + kept[0] in text and "again: ducktest5 run --id" in text


@pytest.mark.parametrize("keep", [False, True])
def test_a_batch_dir_stays_only_when_kept_or_not_all_passed(tmp_path, keep):
    _, run_dir, _, _ = go(tmp_path, [sql_test("a.test")], keep=keep)
    assert (run_dir / "batches" / "b001").exists() == keep


def test_a_batch_dir_with_a_failure_stays(tmp_path):
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test"), sql_test("b_fail.test")])
    assert (run_dir / "batches" / "b001" / "output.txt").exists()
    assert "Wrong result in query" in report["tests"][1]["problems"][0]


# -----------------------------------------------------------------------------
# Whatever goes wrong outside a body is an ERROR of the tests it hit
#


def test_a_clean_that_raises_is_an_error_and_the_run_goes_on(tmp_path):
    store = FakeStore(clean_raises=True)
    tests = [py_test(writes, services=["store"]), py_test(passes, path="test/next.py")]
    code, _, report, _ = go(tmp_path, tests, services=[store.service()])
    assert code == 1
    assert outcomes(report) == {"writes": "error", "passes": "passed"}
    first = result_of(report, "writes")
    assert "could not be removed: RuntimeError: access denied" in first["problems"][0]
    assert first["kept"] == [first["problems"][0].split(" at ")[1].split(" could")[0]]


def test_a_batch_that_cannot_start_is_an_error_of_its_tests_and_python_tests_still_run(tmp_path):
    tests = [sql_test("a.test"), sql_test("b.test"), py_test(passes)]
    code, _, report, _ = go(tmp_path, tests, unittest_bin=str(tmp_path / "no-such-binary"))
    assert code == 1
    assert outcomes(report) == {"a": "error", "b": "error", "passes": "passed"}
    assert "the batch could not run: FileNotFoundError" in result_of(report, "a")["problems"][0]


def test_a_test_dir_that_cannot_be_made_is_an_error_of_that_test(tmp_path, monkeypatch):
    test = py_test(passes)
    real = os.makedirs
    monkeypatch.setattr(
        os,
        "makedirs",
        lambda p, **kw: (_ for _ in ()).throw(OSError("disk full")) if test.dir_name in p else real(p, **kw),
    )
    _, _, report, _ = go(tmp_path, [test, py_test(passes, path="test/other.py")])
    assert sorted(c["outcome"] for c in report["tests"]) == ["error", "passed"]
    broken = next(c for c in report["tests"] if c["outcome"] == "error")
    assert "could not be set up: OSError: disk full" in broken["problems"][0]


def test_the_report_is_written_and_services_are_released_when_the_run_raises(tmp_path):
    engine = FakeEngine()
    with pytest.raises(UsageError, match="no unittest binary"):
        run(
            [sql_test("a.test"), py_test(passes, engines=["fake"])],
            Project(str(tmp_path), {"fake": engine.engine()}, {}, {}, {}, ()),
            duckdb_bin="no-duckdb",
            unittest_bin=None,
            temp_root=str(tmp_path / "runs"),
            out=io.StringIO(),
            session_factory=FakeSession,
        )
    (run_dir,) = (tmp_path / "runs").iterdir()
    assert yaml.safe_load((run_dir / "05-report.yaml").read_text())["counts"]["passed"] == 0
    assert engine.calls == ["boot", "stop"]


def test_each_test_has_one_event_line(tmp_path):
    variant = Variant("oss")
    _, run_dir, _, _ = go(tmp_path, [sql_test("a.test", {"backend": variant}), py_test(fails)])
    ended = end_events(run_dir)
    assert all(isinstance(e["seconds"], float) for e in ended)
    # a test with no variant carries no `variants`: what is not there was not known, never empty
    assert {e["id"]: (e["outcome"], e["runner"], e.get("variants", {})) for e in ended} == {
        "a.test::a[backend=oss]": ("passed", "unittest", {"backend": "oss"}),
        "test/body.py::fails": ("failed", "python", {}),
    }


# -----------------------------------------------------------------------------
# A body: checks, abort, teardowns
#


def ctx_for(test, tmp_path, *, engine=None, session=None, services=None, **kwargs):
    kwargs.setdefault("session_factory", FakeSession)
    kwargs.setdefault("test_env", resolve(test, services))
    return Ctx(
        test,
        temp_dir=str(tmp_path),
        duckdb_bin="no-duckdb",
        duckdb_session=session or FakeSession(),
        engines={"fake": engine or FakeEngine()},
        services=services,
        **kwargs,
    )


def test_failed_checks_are_reported_before_the_abort_and_the_outcome_is_failed(tmp_path):
    def body(ctx):
        c = ctx.engine("fake")
        c.expects("SELECT 1", [2], on_fail="continue")
        c.expects("SELECT 1", [3], on_fail="continue")
        raise ValueError("then the body broke")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "failed"
    assert ["2" in problems[0], "3" in problems[1], "then the body broke" in problems[2]] == [True] * 3
    assert f"at {__file__}:" in problems[0]


def test_failed_checks_alone_fail_the_test(tmp_path):
    def body(ctx):
        ctx.engine("fake").expects("SELECT 1", [2], on_fail="continue")

    test = py_test(body, engines=["fake"])
    assert _run_body(test, ctx_for(test, tmp_path))[0] == "failed"


def test_a_failed_check_stays_a_failure_when_a_setup_fails_after_it(tmp_path):
    def body(ctx):
        c = ctx.engine("fake")
        c.expects("SELECT 1", [2], on_fail="continue")
        c.setup("SELECT boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "failed"
    assert "wrong result" in problems[0] and problems[1].startswith("setup failed")


def test_a_failed_setup_is_an_error_not_a_failure(tmp_path):
    def body(ctx):
        ctx.engine("fake").setup("SELECT boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "error" and problems[0].startswith("setup failed")


def test_a_body_can_branch_on_a_statement_that_failed_and_the_test_still_passes(tmp_path):
    seen = []

    def body(ctx):
        c = ctx.engine("fake")
        seen.append(c.exec("SELECT 1"))
        seen.append(c.exec("SELECT boom"))

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert (outcome, problems) == ("passed", [])
    assert [bool(t) for t in seen] == [True, False]
    assert (seen[0].rows, seen[1].error) == ([(1,)], "boom")


@pytest.mark.parametrize("aborts", [False, True])
def test_teardowns_run_in_reverse_order_also_after_an_abort(tmp_path, aborts):
    engine = FakeEngine()

    def body(ctx):
        c = ctx.engine("fake")
        c.teardown("DROP first")
        c.teardown("DROP second")
        c.oks("SELECT boom" if aborts else "SELECT 1")

    test = py_test(body, engines=["fake"])
    outcome, _, _ = _run_body(test, ctx_for(test, tmp_path, engine=engine))
    assert outcome == ("failed" if aborts else "passed")
    assert engine.statements[-2:] == ["DROP second", "DROP first"]


def test_a_teardown_that_fails_is_an_error_and_the_others_still_run(tmp_path):
    engine = FakeEngine()

    def body(ctx):
        c = ctx.engine("fake")
        c.teardown("DROP last")
        c.teardown("DROP boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path, engine=engine))
    assert outcome == "error" and problems[0].startswith("teardown failed")
    assert engine.statements[-1] == "DROP last"


def test_an_engine_the_test_did_not_declare_is_refused(tmp_path):
    ctx = ctx_for(py_test(passes), tmp_path)
    with pytest.raises(KeyError, match="not declared by this test"):
        ctx.engine("fake")


# -----------------------------------------------------------------------------
# ctx.run_test(): the .test file beside a Python body
#


def runs_its_file(ctx):
    ctx.run_test(env={"EXTRA": "x"})


@pytest.mark.parametrize(
    "body_file, outcome, text",
    [
        ("test/p_env.test", "passed", None),
        ("test/p_fail.test", "failed", "Wrong result in query"),
        ("test/p_skip.test", "error", "was skipped inside the binary: require-env NOT_SET"),
    ],
)
def test_a_paired_test_runs_its_file_through_the_binary(tmp_path, body_file, outcome, text):
    test = py_test(runs_its_file, body_file=body_file, values={"catalog": "duck"})
    _, run_dir, report, _ = go(tmp_path, [test], keep=True)
    assert report["tests"][0]["outcome"] == outcome
    if text:
        assert text in report["tests"][0]["problems"][0]
    else:
        output = (run_dir / "tests" / test.dir_name / "run_test" / "output.txt").read_text()
        assert 'ENV {"CATALOG": "duck", "EXTRA": "x"}' in output


def asks_for_a_binary_that_is_not_there(ctx):
    ctx.run_test()


def gives_rows_the_wrong_shape(ctx):
    ctx.duckdb().expects("SELECT 1", 1)


@pytest.mark.parametrize(
    "body, text",
    [
        (asks_for_a_binary_that_is_not_there, "no unittest binary was found"),
        (gives_rows_the_wrong_shape, "is not rows"),
    ],
)
def test_a_usage_error_in_a_body_is_an_error_of_the_test_and_the_run_goes_on(tmp_path, body, text):
    tests = [py_test(body), py_test(passes, path="test/next.py")]
    _, _, report, _ = go(tmp_path, tests, unittest_bin=None)
    assert outcomes(report) == {body.__name__: "error", "passes": "passed"}
    problem = result_of(report, body.__name__)["problems"][0]
    assert problem.startswith("usage error of the test, not of the command line\n") and text in problem


def test_run_test_in_a_body_with_no_file_says_what_it_is_for(tmp_path):
    with pytest.raises(RuntimeError, match="a .test file of the same stem"):
        ctx_for(py_test(passes), tmp_path).run_test()


# -----------------------------------------------------------------------------
# ctx.location() and ctx.service()
#


def provisioned_store():
    service = FakeStore().service()
    return {"store": Provisioned(service, service.block(), EXISTING)}


def test_a_location_is_under_the_temp_dir_unless_a_service_is_named(tmp_path):
    test = py_test(passes, services=["store"])
    ctx = ctx_for(test, tmp_path, services=provisioned_store(), remote_parts=("run1", "test1"))
    assert ctx.location("tbl") == str(tmp_path / "tbl")
    assert ctx.remote_locations == []
    assert ctx.location("tbl", on="store") == "fake://run1/test1/tbl"
    assert ctx.remote_locations == [("store", "fake://run1/test1/tbl")]
    assert ctx.service("store")["endpoint"] == "http://store"


def test_a_variant_can_put_every_location_on_a_service(tmp_path):
    variant = Variant("s3", services=("store",), location_on="store")
    ctx = ctx_for(py_test(passes, {"k": variant}), tmp_path, services=provisioned_store(), remote_parts=("r", "c"))
    assert ctx.location("tbl") == "fake://r/c/tbl"


def test_a_service_the_test_did_not_declare_is_refused(tmp_path):
    ctx = ctx_for(py_test(passes), tmp_path, services=provisioned_store())
    with pytest.raises(KeyError, match="not declared by this test"):
        ctx.location("tbl", on="store")


# -----------------------------------------------------------------------------
# What a variant brings reaches DuckDB as preamble text
#


def test_the_test_environment_comes_before_the_bodys_own_preamble_in_the_order_of_the_init_file(tmp_path):
    seen = []

    def body(ctx):
        ctx.duckdb("SELECT 1;")
        seen.extend(ctx._duckdb_session.preambles)

    variant = Variant(
        "s3",
        services=("store",),
        requires=("httpfs",),
        settings={"threads": 2, "s3_region": "it's"},
        init_sql=lambda blocks: f"CREATE SECRET (ENDPOINT '{blocks['store']['endpoint']}')",
    )
    code, _, _, _ = go(tmp_path, [py_test(body, {"k": variant})], services=[FakeStore().service()])
    assert code == 0
    assert seen == [
        "LOAD httpfs;\nSET threads = '2';\nSET s3_region = 'it''s';\n"
        "CREATE SECRET (ENDPOINT 'http://store');\nSELECT 1;"
    ]


def test_a_python_body_reads_the_roots_and_puts_what_it_makes_under_the_temp_root(tmp_path):
    seen = {}

    def body(ctx):
        seen["roots"] = ctx.roots
        seen["location"] = ctx.location("tbl")

    variant = Variant("http", roots={"temp_dir_root": "http://origin/temp", "local_temp_dir_root": "/www/temp"})
    test = py_test(body, {"storage": variant})
    assert go(tmp_path, [test])[0] == 0
    assert seen["roots"] == {"temp_dir_root": "http://origin/temp", "local_temp_dir_root": "/www/temp"}
    assert seen["location"].startswith("http://origin/temp/") and seen["location"].endswith(f"/{test.dir_name}/tbl")


def test_a_test_whose_environment_differs_from_the_runs_gets_a_shell_of_its_own(tmp_path):
    made, seen = [], {}

    def factory(binary, *, home=None, env=None):
        made.append(FakeSession(binary, home=home, env=env))
        return made[-1]

    def body(ctx):
        seen["shared"] = ctx.duckdb() is not None
        seen["is_the_runs_shell"] = ctx._duckdb_session in made[1:]

    variant = Variant("no-key", env={"AWS_PROFILE": "ducktest"}, env_unset=("AWS_ACCESS_KEY_ID",))
    assert go(tmp_path, [py_test(body, {"auth": variant})], session_factory=factory)[0] == 0
    run_shell, own = made  # the run's own shell, then the one this test had to have
    assert (run_shell.env, own.env["AWS_PROFILE"]) == (None, "ducktest")
    assert "AWS_ACCESS_KEY_ID" not in own.env
    assert seen["is_the_runs_shell"] is False


def test_a_preamble_that_fails_is_a_setup_error(tmp_path):
    def body(ctx):
        ctx.duckdb()

    _, _, report, _ = go(tmp_path, [py_test(body)], session_factory=lambda b, **kw: FakeSession(preamble_fails=True))
    assert report["tests"][0]["outcome"] == "error"
    assert "the preamble failed: Catalog Error" in report["tests"][0]["problems"][0]


def test_an_unknown_session_kind_is_refused(tmp_path):
    with pytest.raises(ValueError, match="'per_run', 'per_client', 'per_call'"):
        ctx_for(py_test(passes), tmp_path).duckdb(session="per_test")


def test_an_own_session_is_a_warm_shell_of_its_own_closed_when_the_test_ends(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        session = FakeSession(binary, home=home, env=env)
        session.close = lambda: made_closed.append(session)
        made.append(session)
        return session

    made_closed = []

    def body(ctx):
        shared = ctx.duckdb("LOAD delta;")
        first = ctx.duckdb("LOAD delta;", session="per_client")
        second = ctx.duckdb("LOAD delta;", session="per_client")
        first.oks("SELECT 1")
        second.oks("SELECT 2")
        shared.oks("SELECT 3")

    code, _, _, _ = go(tmp_path, [py_test(body)], session_factory=factory)
    assert code == 0
    run_shell, first, second = made  # the run's shell, then one for each `per_client` client
    assert [first.statements, second.statements, run_shell.statements] == [["SELECT 1"], ["SELECT 2"], ["SELECT 3"]]
    assert [first.preambles, second.preambles] == [run_shell.preambles] * 2
    assert [first.home, second.home] == [run_shell.home] * 2
    assert made_closed[:2] == [second, first]  # both before the run's own shell goes away


# -----------------------------------------------------------------------------
# Retries
#


def test_a_flaky_file_is_passed_with_its_attempts_in_the_event_line_and_its_output_stays(tmp_path, flaky_state):
    tests = [sql_test("a.test"), sql_test("b_flaky2.test", retries=1), sql_test("c_flaky2.test")]
    code, run_dir, report, text = go(tmp_path, tests)
    assert outcomes(report) == {"a": "passed", "b_flaky2": "passed", "c_flaky2": "failed"}
    assert sorted(e.get("attempts") for e in end_events(run_dir) if e.get("attempts")) == [2]
    assert "[passed on attempt 2 of 2]" in text


def test_the_output_of_a_failed_attempt_stays_when_a_later_attempt_passed(tmp_path, flaky_state):
    code, run_dir, _, _ = go(tmp_path, [sql_test("b_flaky2.test", retries=1)])
    assert code == 0
    assert "Wrong result in query" in (run_dir / "batches" / "b001" / "output.txt").read_text()


def test_a_python_body_with_retries_runs_again_in_a_directory_of_its_own(tmp_path):
    seen = []

    def flaky(ctx):
        seen.append((os.path.basename(ctx.temp_dir), os.listdir(ctx.temp_dir), ctx.location("t", on="store")))
        open(os.path.join(ctx.temp_dir, "left-behind"), "w").close()
        assert len(seen) == 2

    store = FakeStore()
    test = py_test(flaky, services=["store"], retries=2)
    code, run_dir, report, _ = go(tmp_path, [test], services=[store.service()])
    assert code == 0
    assert [(name, files) for name, files, _ in seen] == [(test.dir_name, []), (test.dir_name + ".attempt2", [])]
    assert (report["tests"][0]["attempts"], report["tests"][0]["note"]) == (2, "passed on attempt 2 of 3")
    assert store.cleaned == [uri for _, _, uri in seen] and len(set(store.cleaned)) == 2
    assert list((run_dir / "tests").iterdir()) == []


def test_a_python_body_that_never_passes_keeps_its_first_failure(tmp_path):
    seen = []

    def never(ctx):
        seen.append(1)
        assert False, f"attempt {len(seen)}"

    _, run_dir, report, _ = go(tmp_path, [py_test(never, retries=1)])
    test = report["tests"][0]
    assert (test["outcome"], test["attempts"], len(test["kept"])) == ("failed", 2, 2)
    assert "attempt 1" in test["problems"][0] and len(test["problems"]) == 1


def test_duckdb_gets_a_home_under_the_run_dir_for_both_kinds_of_session(tmp_path):
    seen = {}

    def body(ctx):
        seen["session"] = ctx._duckdb_session.home
        seen["per_call"] = ctx.duckdb(session="per_call")._run.__self__._home.path

    _, run_dir, _, _ = go(tmp_path, [py_test(body)])
    assert seen == {"session": str(run_dir / "home"), "per_call": str(run_dir / "home")}


def test_the_timeout_of_a_duckdb_client_reaches_each_of_its_statements(tmp_path):
    seen = []

    def body(ctx):
        ctx.duckdb(timeout=7).oks("SELECT 1")
        ctx.duckdb().oks("SELECT 2")
        seen.extend(ctx._duckdb_session.timeouts)
        seen.append(ctx.duckdb(session="per_call", timeout=9)._run.__self__._timeout)

    assert go(tmp_path, [py_test(body)])[0] == 0
    assert seen == [7, 120, 9]


def test_the_scan_record_names_the_version_of_an_engine(tmp_path):
    project = Project(str(tmp_path), {"fake": FakeEngine().engine()}, {}, {}, {}, ())
    record = py_test(passes, engines=["fake"]).record(project)
    assert record["preconditions"]["engines"] == [{"id": "fake", "version": "fake:1"}]


# -----------------------------------------------------------------------------
# The side outputs of a run: live on the terminal, files beside it
#


def test_a_run_writes_the_report_files_it_is_asked_for(tmp_path):
    import xml.etree.ElementTree as ET

    out_json, out_xml = str(tmp_path / "r.json"), str(tmp_path / "r.xml")
    _, run_dir, _, text = go(
        tmp_path, [sql_test("a.test"), sql_test("b_fail.test")], report_json=out_json, junit_file=out_xml
    )
    assert "PASSED   a.test::a" in text  # the live output still goes to the terminal
    assert json.loads(open(out_json).read()) == json.loads((run_dir / "05-report.json").read_text())
    root = ET.parse(out_xml).getroot()
    assert sorted(s.attrib["name"] for s in root) == ["a.test", "b_fail.test"]


def test_a_terse_run_prints_blocks_and_one_summary_line_instead_of_the_live_output(tmp_path):
    _, _, _, text = go(tmp_path, [sql_test("a.test"), sql_test("b_fail.test")], level="terse")
    assert "PASSED" not in text
    assert text.splitlines()[0] == "test: b_fail.test::b_fail"
    assert text.splitlines()[-1].startswith("summary: 1 passed, 1 failed, 0 error, 0 blocked, 0 skipped in ")
