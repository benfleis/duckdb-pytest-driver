"""ducktest's run loop, with no DuckDB and no Spark: a fake engine, a fake service, a fake DuckDB
shell and the fake unittest binary."""

import io
import json
import os
import re
import sys

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import yaml

from ducktest import phases
from ducktest.cli import main
from ducktest.binary import UsageError
from ducktest.resources.client import SqlError
from ducktest.conf import LoadedProject
from ducktest.ctx import Context
from ducktest.events import TEST_END
from ducktest.events import read as read_events
from ducktest.model import PAIRED, PYTHON, SQLLOGIC, Test, TestDef, Value
from ducktest.options import Options
from ducktest.provision import EXISTING
from ducktest.engines.duckdb import DuckEngineDef
from ducktest.resources import ON_FAILURE, RW, BootedEngineDef, Service, ServiceDef, StorageDef
from ducktest.resources.engine import Runtime
from ducktest.resources.made import Made
from ducktest.resources.storage import At
from ducktest.run import _binary_failure, _run_body, run
from ducktest.testenv import resolve

FAKE = os.path.join(os.path.dirname(__file__), "ducktest_fakes", "fake_unittest.py")
FAKE_DEBUGGER = os.path.join(os.path.dirname(__file__), "ducktest_fakes", "fake_debugger.py")


class FakeEngine:
    """An engine that answers every statement with one row, and records what it was asked."""

    def __init__(self, *, boots=True):
        self.boots, self.calls, self.statements = boots, [], []

    def engine(self, name="fake"):
        return BootedEngineDef(name, version="fake:1", boot=self._boot)

    def _boot(self, log_path):
        self.calls.append("boot")
        if not self.boots:
            raise RuntimeError("no JVM")
        return self

    def begin_test(self):
        self.calls.append("begin_test")

    def run(self, statements):
        self.statements += statements
        if any("boom" in s for s in statements):
            raise SqlError("boom", statement=len(statements) - 1)
        return ["v"], [(1,)]

    def stop(self):
        self.calls.append("stop")


@dataclass(eq=False)
class StoreDef(ServiceDef):
    """A service found running at `address`, never started; `up` says whether it answers. Its
    `token` is a secret."""

    kind = "store"
    attributes = ("endpoint",)
    secret_attributes = ("token",)

    name: str = "store"
    up: bool = True
    address: str | None = "http://store"
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE


class Store(Service):
    def read(self):
        return {"endpoint": self.address, "token": "s3cr3t"} if self.definition.up else None


StoreDef.instance = Store


@dataclass(eq=False)
class PartsStoreDef(StoreDef):
    """A store whose parts are its storages, by id; it refuses the part `refuses`."""

    kind = "parts_store"
    refuses: str = "bad"

    def parts_needed(self, inputs):
        return [d.name for d in inputs if isinstance(d, StorageDef)]


class PartsStore(Store):
    @property
    def refused(self):
        return {self.definition.refuses: "bucket bad: it holds other content"}


PartsStoreDef.instance = PartsStore


class FakeShell:
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

    def close(self):
        pass


def matrix_value(name, **options):
    """A value of a matrix parameter; the key it is given under in a cell names the parameter."""
    return name, Options.of(options, f"matrix value {name!r}", leave_out=("matrix",))


def cell(given):
    return {p: Value(p, name, options) for p, (name, options) in (given or {}).items()}


def bound(test, inputs, slots):
    """`test` holding `inputs`, and the storages `slots` names (storage/data/temp -> a StorageDef or
    `STORAGE.at(...)`) in those roles, as a scan binds them."""
    held = list(inputs)
    for value in (v for v in slots.values() if v is not None):
        d = value.storage if isinstance(value, At) else value
        if all(d is not h for h in held):
            held.append(d)
    test.inputs = tuple((d, RW) for d in held)
    test.roles = {
        slot: (v.storage, v.path) if isinstance(v, At) else (v, None) for slot, v in slots.items() if v is not None
    }
    return test


def sql_test(path, given=None, *, inputs=(), storage=None, data=None, temp=None, **fields):
    name = os.path.splitext(os.path.basename(path))[0]
    definition = TestDef(path=path, name=name, type=SQLLOGIC, body_file=path, **fields)
    return bound(Test(definition, cell(given)), inputs, {"storage": storage, "data": data, "temp": temp})


def py_test(
    body, given=None, *, path="test/body.py", body_file=None, inputs=(), storage=None, data=None, temp=None, **fields
):
    definition = TestDef(
        path=path, name=body.__name__, type=PAIRED if body_file else PYTHON, body=body, body_file=body_file, **fields
    )
    return bound(Test(definition, cell(given)), inputs, {"storage": storage, "data": data, "temp": temp})


def go(tmp_path, tests, *, engines=(), unittest_bin=FAKE, duckdb_bin="no-duckdb", **kwargs):
    """Returns (exit code, run dir, report, printed text)."""
    storages = {d.name: d for t in tests for d, _ in t.inputs if isinstance(d, StorageDef)}
    project = LoadedProject(str(tmp_path), {e.name: e for e in engines}, {}, {}, {}, storages)
    out = io.StringIO()
    kwargs.setdefault("shell_factory", FakeShell)
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
    return {c["name"].split("::")[-1]: c["outcome"] for c in report["tests"]}


def result_of(report, name):
    """One test of the report by its name. Tests are listed in the order they ended, which with
    several workers is not the order they were given in."""
    return next(c for c in report["tests"] if c["name"].split("::")[-1] == name)


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
    code, _, report, _ = go(tmp_path, [py_test(passes), py_test(passes, inputs=[StoreDef(up=False)], path="test/s.py")])
    assert code == 1
    assert sorted(c["outcome"] for c in report["tests"]) == ["blocked", "passed"]


# -----------------------------------------------------------------------------
# Batches
#


def batch_of(report):
    return {c["name"].split("::")[-1]: c["batch"] for c in report["tests"]}


def test_tests_of_one_file_never_share_a_batch(tmp_path):
    values = {"a": matrix_value("a", tags=("x",)), "b": matrix_value("b", tags=("y",))}
    tests = [
        sql_test("t.test", {"k": values["a"]}),
        sql_test("t.test", {"k": values["b"]}),
        sql_test("u.test", {"k": values["a"]}),
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
    store = StoreDef()
    tests = [sql_test("a.test"), sql_test("b.test", inputs=[store]), sql_test("c_flaky2.test", inputs=[store])]
    _, _, report, _ = go(tmp_path, tests)
    assert len(set(batch_of(report).values())) == 3
    assert outcomes(report)["c_flaky2"] == "failed"
    assert (tmp_path / "state" / "c_flaky2.test.runs").read_text() == "1"


def test_a_matrix_value_init_sql_is_the_batch_init_file(tmp_path):
    value = matrix_value("s3", init_sql="ATTACH 'http://store'")
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test", {"k": value})], keep=True)
    init = run_dir / report["tests"][0]["batch"] / "init.sqllogic"
    assert init.read_text() == "statement ok\nATTACH 'http://store';\n"


def test_requires_settings_and_init_sql_reach_the_init_file_in_that_order(tmp_path):
    value = matrix_value(
        "curl",
        requires=("httpfs", "json"),
        settings={"httpfs_client_implementation": "curl"},
        init_sql="CREATE SECRET s (TYPE S3)",
    )
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test", {"client": value})], keep=True)
    init = run_dir / report["tests"][0]["batch"] / "init.sqllogic"
    assert init.read_text() == (
        "require httpfs\n\nrequire json\n\n"
        "statement ok\nSET httpfs_client_implementation = 'curl';\n\n"
        "statement ok\nCREATE SECRET s (TYPE S3);\n"
    )


def test_a_matrix_value_sets_and_unsets_variables_in_the_environment_of_the_unittest_process(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "LEAKED-FROM-THE-CALLER")
    value = matrix_value("env", process_env={"AWS_PROFILE": "ducktest", "AWS_ACCESS_KEY_ID": None})
    _, run_dir, report, _ = go(tmp_path, [sql_test("a_showenv.test", {"auth": value})], keep=True)
    output = (run_dir / report["tests"][0]["batch"] / "output.txt").read_text()
    printed = json.loads(output.split("PROCESS_ENV ")[1].splitlines()[0])
    assert printed == {"AWS_PROFILE": "ducktest", "AWS_ACCESS_KEY_ID": None}


def test_tests_with_other_environments_never_share_a_unittest_process(tmp_path):
    values = {
        "plain": matrix_value("plain"),
        "loaded": matrix_value("loaded", requires=("httpfs",)),
        "set": matrix_value("set", settings={"threads": 1}),
        "noisy": matrix_value("noisy", process_env={"AWS_PROFILE": "p"}),
    }
    tests = [sql_test(f"t{n}.test", {"k": v}) for n, v in enumerate(values.values())]
    _, _, report, _ = go(tmp_path, tests + [sql_test("t9.test", {"k": values["plain"]})], keep=True)
    batches = batch_of(report)
    assert len(set(batches.values())) == 4  # one for each environment; the two plain ones ride along
    assert batches["t0[k=plain]"] == batches["t9[k=plain]"]


SECRET = matrix_value("s3", init_sql="CREATE SECRET (SECRET 'hunter2')")


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
    assert (run_dir / "tests" / test.dir_name / "run_paired" / "init.sqllogic").exists()
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


def debug(tmp_path, tests, command, **kwargs):
    project = LoadedProject(str(tmp_path), {}, {}, {}, {})
    out = io.StringIO()
    code = run(
        tests,
        project,
        duckdb_bin="no-duckdb",
        unittest_bin=FAKE,
        working_dir=str(tmp_path),
        temp_root=str(tmp_path / "runs"),
        out=out,
        shell_factory=FakeShell,
        debugger=command,
        **kwargs,
    )
    return code, out.getvalue()


def test_the_debugger_gets_the_binary_and_the_batch_a_normal_run_would_have_used(tmp_path, debugger):
    command, started = debugger
    test = sql_test("a.test", {"storage": MADE_OF}, values={"catalog": "duck"})
    code, text = debug(tmp_path, [test], command)
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


MADE_OF = matrix_value(
    "s3",
    values={"root": "s3://bucket"},
    settings={"s3_region": "eu"},
    init_sql="CREATE SECRET (SECRET 'hunter2')",
)


def test_a_failed_test_says_what_it_was_made_of(tmp_path):
    test = sql_test("b_fail.test", {"storage": MADE_OF}, inputs=[StoreDef()])
    _, run_dir, report, text = go(tmp_path, [test])
    facts = report["tests"][0]["made_of"]
    assert facts["environment"] == [
        {"what": "set", "is": "s3_region=eu", "from": "matrix storage=s3"},
        {"what": "init sql", "is": "", "from": "matrix storage=s3"},
        {"what": "values", "is": "storage=s3", "from": "matrix storage"},
        {"what": "values", "is": "root=s3://bucket", "from": "matrix storage=s3"},
        {"what": "values", "is": "STORE_ENDPOINT=http://store", "from": "service store"},
        {"what": "root", "is": f"temp_dir_root={run_dir}", "from": "default storage"},
        {"what": "root", "is": f"data_dir={tmp_path}/data", "from": "default storage"},
    ]
    assert {name: os.path.basename(p) for name, p in facts["files"].items()} == {
        "config": "config.json",
        "tests": "tests.txt",
        "init": "init.sqllogic",
        "output": "output.txt",
    }
    assert all(os.path.isfile(p) for p in facts["files"].values())
    block = text.split("--- " + test.name)[1]
    assert "\n  values: root=s3://bucket  <- matrix storage=s3\n" in block
    assert "\n  set: s3_region=eu  <- matrix storage=s3\n" in block
    assert "\n  init sql  <- matrix storage=s3\n" in block
    assert "\n  values: STORE_ENDPOINT=http://store  <- service store\n" in block
    assert f"\n  init: {facts['files']['init']}\n" in block
    assert "\n  again: ducktest run --id " in block
    assert "hunter2" not in text  # the init SQL is named by its source, never printed


def test_a_failure_says_what_the_environment_of_the_process_was(tmp_path):
    value = matrix_value(
        "env",
        requires=("httpfs",),
        process_env={"AWS_PROFILE": "ducktest", "AWS_SECRET_ACCESS_KEY": "hunter2", "AZURE_CLIENT_SECRET": None},
    )
    test = sql_test("b_fail.test", {"auth": value})
    _, _, _, text = go(tmp_path, [test])
    block = text.split("--- " + test.name)[1]
    assert "\n  require: httpfs  <- matrix auth=env\n" in block
    assert "\n  process env: AWS_PROFILE=ducktest  <- matrix auth=env\n" in block
    assert "\n  process env: AWS_SECRET_ACCESS_KEY=***  <- matrix auth=env\n" in block  # the name says it is one
    assert "\n  process env unset: AZURE_CLIENT_SECRET  <- matrix auth=env\n" in block
    assert "hunter2" not in text


def test_a_passed_test_says_nothing_of_the_kind(tmp_path):
    _, _, report, text = go(tmp_path, [sql_test("a.test", {"storage": MADE_OF}, inputs=[StoreDef()])])
    assert "made_of" not in report["tests"][0]
    assert "values: " not in text and "config: " not in text


def test_a_failed_python_test_names_its_test_directory(tmp_path):
    test = py_test(writes_and_fails, inputs=[StoreDef()])
    _, run_dir, report, text = go(tmp_path, [test])
    facts = report["tests"][0]["made_of"]
    assert facts["files"] == {"dir": str(run_dir / "tests" / test.dir_name)}
    assert f"\n  dir: {facts['files']['dir']}\n" in text


def test_a_failed_python_test_s_root_is_its_own_place_not_the_run_s(tmp_path):
    """The run level (`scan --env`) keeps `temp_dir_root`; a Python test's own place is TEMP_DIR."""
    test = py_test(fails)
    _, run_dir, report, text = go(tmp_path, [test])
    facts = report["tests"][0]["made_of"]
    own = str(run_dir / "tests" / test.dir_name)
    assert {"what": "root", "is": f"TEMP_DIR={own}", "from": "default storage"} in facts["environment"]
    assert "temp_dir_root=" not in text
    assert f"\n  root: TEMP_DIR={own}  <- default storage\n" in text


def test_a_secret_of_a_service_never_reaches_what_the_test_was_made_of(tmp_path):
    test = sql_test("b_fail.test", values={"token": "s3cr3t"}, inputs=[StoreDef()])
    _, _, report, text = go(tmp_path, [test])
    assert {"what": "values", "is": "token=***", "from": "@test"} in report["tests"][0]["made_of"]["environment"]
    assert "s3cr3t" not in text


def test_the_event_line_of_a_failure_holds_the_same_facts(tmp_path):
    test = sql_test("b_fail.test", {"storage": MADE_OF}, inputs=[StoreDef()])
    _, run_dir, report, _ = go(tmp_path, [test])
    (event,) = end_events(run_dir)
    assert event["made_of"] == report["tests"][0]["made_of"]


# -----------------------------------------------------------------------------
# A failure in the init SQL is named, and is not blamed on the body
#


def test_a_failure_in_the_init_sql_is_an_error_that_names_the_matrix_value_and_the_file(tmp_path):
    test = sql_test("a_initfail.test", {"storage": matrix_value("s3", init_sql="ATTACH 'nowhere'")})
    code, run_dir, report, text = go(tmp_path, [test], keep=True)
    assert (code, report["tests"][0]["outcome"]) == (1, "error")
    first_line = report["tests"][0]["problems"][0].splitlines()[0]
    assert first_line == f"in the init SQL of matrix storage=s3 ({run_dir / 'batches' / 'b001' / 'init.sqllogic'}:1)"
    assert "a_initfail.test:1" not in first_line  # not the body file, where the reader would find nothing
    assert first_line in text


def test_a_failed_statement_shows_duckdbs_error_under_the_sql(tmp_path):
    _, _, report, text = go(tmp_path, [sql_test("a_initfail.test", settings={"threads": 2})])
    assert report["tests"][0]["failure"]["error"] == "Catalog Error: no such thing"
    assert re.search(r"  sql:\n    SET threads = '2';\n  error:\n    Catalog Error: no such thing\n", text), text


def test_the_failing_statement_of_the_init_sql_names_its_own_source(tmp_path):
    given = {
        "backend": matrix_value("oss", init_sql="SET a = 1"),
        "storage": matrix_value("s3", init_sql="SET b = 2"),
    }
    _, _, report, _ = go(tmp_path, [sql_test("a_initfail2.test", given)], keep=True)
    assert "in the init SQL of matrix storage=s3 (" in report["tests"][0]["problems"][0]


def test_a_setting_that_fails_in_the_init_sql_names_the_rule_that_asked_for_it(tmp_path):
    test = sql_test("a_initfail.test", settings={"threads": 2})
    test.definition.sources = {("settings", "threads"): "ducktest_conf.py: default"}
    _, _, report, _ = go(tmp_path, [test])
    assert "in the init SQL of ducktest_conf.py: default (" in report["tests"][0]["problems"][0]


def test_a_failure_in_the_body_stays_a_failure_when_the_batch_has_init_sql(tmp_path):
    test = sql_test("b_fail.test", {"storage": matrix_value("s3", init_sql="ATTACH 'nowhere'")})
    _, _, report, _ = go(tmp_path, [test])
    assert report["tests"][0]["outcome"] == "failed"


# -----------------------------------------------------------------------------
# What does not come up blocks its tests, and only those
#


def test_a_service_that_is_down_blocks_its_tests_and_their_engine_is_not_booted(tmp_path):
    engine = FakeEngine()
    tests = [py_test(passes, inputs=[StoreDef(up=False)], engines=["fake"]), py_test(passes, path="test/free.py")]
    code, run_dir, report, text = go(tmp_path, tests, engines=[engine.engine()])
    assert [c["outcome"] for c in report["tests"]] == ["blocked", "passed"]
    assert report["tests"][0]["blocked_by"] == ["store"]
    assert "does not answer" in report["not_provisioned"]["store"]
    assert engine.calls == []
    assert "'store' did not come up: 1 test blocked" in text


def test_a_part_the_service_refused_blocks_only_the_tests_that_need_it(tmp_path):
    store = PartsStoreDef()
    good, bad = (StorageDef(n, str(tmp_path / "parts" / n), inputs=[store]) for n in ("good", "bad"))

    def needs_bad(ctx):
        pass

    tests = [py_test(passes, inputs=[good]), py_test(needs_bad, path="test/bad.py", inputs=[bad])]
    _, run_dir, report, _ = go(tmp_path, tests)
    assert outcomes(report) == {"passes": "passed", "needs_bad": "blocked"}
    (blocked,) = [c for c in report["tests"] if c["outcome"] == "blocked"]
    assert blocked["blocked_by"] == ["store/bad"] and "it holds other content" in blocked["note"]
    record = yaml.safe_load((run_dir / "03-provision.yaml").read_text())["services"]["store"]
    assert record["refused"] == {"bad": "bucket bad: it holds other content"}


def test_an_engine_that_does_not_boot_blocks_its_tests(tmp_path):
    engine = FakeEngine(boots=False)
    tests = [py_test(passes, engines=["fake"]), sql_test("a.test")]
    _, _, report, _ = go(tmp_path, tests, engines=[engine.engine()])
    assert outcomes(report) == {"passes": "blocked", "a": "passed"}
    assert "no JVM" in report["not_provisioned"]["fake"]


def test_secrets_of_a_service_are_not_in_the_provision_record(tmp_path):
    _, run_dir, _, _ = go(tmp_path, [py_test(passes, inputs=[StoreDef()])])
    record = yaml.safe_load((run_dir / "03-provision.yaml").read_text())["services"]["store"]
    assert (record["mode"], record["owned"], record["address"]) == (EXISTING, False, "http://store")
    assert record["attributes"] == {"endpoint": "http://store", "token": "***"}


# -----------------------------------------------------------------------------
# Keep and clean
#


def writes(ctx):
    open(os.path.join(ctx.temp_dir, "f"), "w").close()


def writes_and_fails(ctx):
    writes(ctx)
    assert False


@pytest.mark.parametrize("keep", [False, True])
def test_what_a_passed_test_wrote_is_removed_unless_kept(tmp_path, keep):
    code, run_dir, report, _ = go(tmp_path, [py_test(writes)], keep=keep)
    assert code == 0
    if keep:
        assert len(list((run_dir / "tests").iterdir())) == 1
        assert len(report["tests"][0]["kept"]) == 1
    else:
        assert list((run_dir / "tests").iterdir()) == []
        assert "kept" not in report["tests"][0]


def test_what_a_failed_test_wrote_stays_and_is_listed(tmp_path):
    _, run_dir, report, text = go(tmp_path, [py_test(writes_and_fails)])
    kept = report["tests"][0]["kept"]
    assert os.path.isfile(os.path.join(kept[0], "f"))
    assert "kept: " + kept[0] in text and "again: ducktest run --id" in text


@pytest.mark.parametrize("keep", [False, True])
def test_a_batch_dir_stays_only_when_kept_or_not_all_passed(tmp_path, keep):
    _, run_dir, _, _ = go(tmp_path, [sql_test("a.test")], keep=keep)
    assert (run_dir / "batches" / "b001").exists() == keep


def test_a_run_removes_its_prefix_under_a_storage_but_a_failed_test_s_place(tmp_path):
    def writes_to_the_storage(ctx):
        open(os.path.join(ctx.values.temp_dir, "f"), "w").close()

    def writes_to_the_storage_and_fails(ctx):
        writes_to_the_storage(ctx)
        assert False

    storage = StorageDef("scratch", str(tmp_path / "scratch"))
    passed = py_test(writes_to_the_storage, temp=storage, path="test/passed.py")
    failed = py_test(writes_to_the_storage_and_fails, temp=storage, path="test/failed.py")
    _, run_dir, report, _ = go(tmp_path, [passed, failed])
    (prefix,) = (tmp_path / "scratch" / "temp").iterdir()
    assert [p.name for p in prefix.iterdir()] == [failed.dir_name]
    assert str(prefix / failed.dir_name) in result_of(report, "writes_to_the_storage_and_fails")["kept"]

    (tmp_path / "green").mkdir()
    assert go(tmp_path / "green", [passed])[0] == 0
    assert [p.name for p in (tmp_path / "scratch" / "temp").iterdir()] == [prefix.name]


def test_a_failed_test_s_place_in_an_object_store_is_kept(tmp_path):
    def fails(ctx):
        assert False

    failed = py_test(fails, temp=StorageDef("remote", "s3://bucket/lake"), path="test/failed.py")
    _, run_dir, report, _ = go(tmp_path, [failed])
    assert f"s3://bucket/lake/temp/{run_dir.name}/{failed.dir_name}" in result_of(report, "fails")["kept"]


def test_a_batch_dir_with_a_failure_stays(tmp_path):
    _, run_dir, report, _ = go(tmp_path, [sql_test("a.test"), sql_test("b_fail.test")])
    assert (run_dir / "batches" / "b001" / "output.txt").exists()
    assert "Wrong result in query" in report["tests"][1]["problems"][0]


# -----------------------------------------------------------------------------
# Whatever goes wrong outside a body is an ERROR of the tests it hit
#


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
            LoadedProject(str(tmp_path), {"fake": engine.engine()}, {}, {}, {}),
            duckdb_bin="no-duckdb",
            unittest_bin=None,
            temp_root=str(tmp_path / "runs"),
            out=io.StringIO(),
            shell_factory=FakeShell,
        )
    (run_dir,) = (tmp_path / "runs").iterdir()
    assert yaml.safe_load((run_dir / "05-report.yaml").read_text())["counts"]["passed"] == 0
    assert engine.calls == ["boot", "stop"]


def test_each_test_has_one_event_line(tmp_path):
    _, run_dir, _, _ = go(tmp_path, [sql_test("a.test", {"backend": matrix_value("oss")}), py_test(fails)])
    ended = end_events(run_dir)
    assert all(isinstance(e["seconds"], float) for e in ended)
    # a test with no parameters carries no `matrix`: what is not there was not known, never empty
    assert {e["name"]: (e["outcome"], e["runner"], e.get("matrix", {})) for e in ended} == {
        "a.test::a[backend=oss]": ("passed", "duck-sqllogic", {"backend": "oss"}),
        "test/body.py::fails": ("failed", "duck-shell", {}),
    }


# -----------------------------------------------------------------------------
# A body: checks, abort, teardowns
#


def held(live, engine="duck"):
    """An open engine instance on `live`, as the run leases one to a test."""
    d = DuckEngineDef(engine) if engine == "duck" else BootedEngineDef(engine)
    instance = d.make()
    instance.runtime = Runtime(shell=lambda env: live, booted={engine: live})
    instance.start()
    return instance


def ctx_for(test, tmp_path, *, engine=None, shell=None, services=None, test_env=None, **kwargs):
    """A Context with a Client on DuckDB, and on the fake engine where the test uses it."""
    engines = [held(shell or FakeShell())]
    if "fake" in test.engines:
        engines.append(held(engine or FakeEngine(), "fake"))
    return Context(
        test,
        test_env or resolve(test),
        engines,
        temp_dir=str(tmp_path),
        services=services,
        **kwargs,
    )


def test_failed_checks_are_reported_before_the_abort_and_the_outcome_is_failed(tmp_path):
    def body(ctx):
        c = ctx.client("fake")
        c.expects("SELECT 1", [2], on_fail="continue")
        c.expects("SELECT 1", [3], on_fail="continue")
        raise ValueError("then the body broke")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "failed"
    assert ["2" in problems[0], "3" in problems[1], "then the body broke" in problems[2]] == [True] * 3
    assert "at test/body.py:" in problems[0]


class FailingShell(FakeShell):
    def run(self, statements, *, timeout=None):
        raise SqlError("boom", statement=len(statements) - 1)


@pytest.mark.parametrize("client, prefix", [("duck", "[duck/duck-shell]"), ("fake", "[fake]")])
def test_a_failure_names_the_client_and_for_a_duckdb_engine_its_process_kind(tmp_path, client, prefix):
    def body(ctx):
        ctx.client(client).oks("SELECT boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path, shell=FailingShell()))
    assert outcome == "failed"
    assert f"{prefix} statement failed: boom\n    SELECT boom" in problems[0]


def test_the_binary_names_a_failed_file_as_the_test_id_does():
    test = sql_test("test/sql/a.test", root_path="sql/a.test")
    detail = "Wrong result in query! (test/sql/a.test:4)!\nExpected result:\n2\nActual result:\n1"
    assert _binary_failure(test, detail)["where"] == "sql/a.test:4"


def test_failed_checks_alone_fail_the_test(tmp_path):
    def body(ctx):
        ctx.client("fake").expects("SELECT 1", [2], on_fail="continue")

    test = py_test(body, engines=["fake"])
    assert _run_body(test, ctx_for(test, tmp_path))[0] == "failed"


def test_a_failed_check_stays_a_failure_when_a_setup_fails_after_it(tmp_path):
    def body(ctx):
        c = ctx.client("fake")
        c.expects("SELECT 1", [2], on_fail="continue")
        c.setup("SELECT boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "failed"
    assert "wrong result" in problems[0] and problems[1].startswith("setup failed")


def test_a_failed_setup_is_an_error_not_a_failure(tmp_path):
    def body(ctx):
        ctx.client("fake").setup("SELECT boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "error" and problems[0].startswith("setup failed")


def test_a_body_can_branch_on_a_statement_that_failed_and_the_test_still_passes(tmp_path):
    seen = []

    def body(ctx):
        c = ctx.client("fake")
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
        c = ctx.client("fake")
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
        c = ctx.client("fake")
        c.teardown("DROP last")
        c.teardown("DROP boom")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path, engine=engine))
    assert outcome == "error" and problems[0].startswith("teardown failed")
    assert engine.statements[-1] == "DROP last"


def test_an_engine_the_test_did_not_name_is_a_usage_error_that_says_where_to_name_it(tmp_path):
    def body(ctx):
        ctx.client("spark")

    _, _, report, _ = go(tmp_path, [py_test(body)])
    (test,) = report["tests"]
    assert test["outcome"] == "error"
    assert (
        "ctx.client('spark'): this test has clients 'duck'; a DuckDB engine with no declaration is named 'duck'; "
        "any other engine goes in @test(engines=[...])" in test["problems"][0]
    )


# -----------------------------------------------------------------------------
# ctx.run_paired(): the .test file beside a Python body
#


def runs_its_file(ctx):
    ctx.run_paired(values={"EXTRA": "x"})


@pytest.mark.parametrize(
    "body_file, outcome, text",
    [
        ("test/p_env.test", "passed", None),
        ("test/p_fail.test", "failed", "Wrong result in query"),
        ("test/p_skip.test", "skipped", "require-env NOT_SET"),
    ],
)
def test_a_paired_test_runs_its_file_through_the_binary(tmp_path, body_file, outcome, text):
    test = py_test(runs_its_file, body_file=body_file, values={"catalog": "duck"})
    _, run_dir, report, _ = go(tmp_path, [test], keep=True)
    assert report["tests"][0]["outcome"] == outcome
    if text:
        assert text in report["tests"][0]["problems"][0]
    else:
        output = (run_dir / "tests" / test.dir_name / "run_paired" / "output.txt").read_text()
        assert 'ENV {"CATALOG": "duck", "EXTRA": "x"}' in output


def asks_for_a_binary_that_is_not_there(ctx):
    ctx.run_paired()


def gives_rows_the_wrong_shape(ctx):
    ctx.client("duck").expects("SELECT 1", 1)


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


def test_run_paired_in_a_body_with_no_test_file_beside_it_says_so(tmp_path):
    message = (
        "ctx.run_paired() runs the .test file of the body's stem, and there is no test/body.test beside test/body.py"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        ctx_for(py_test(passes), tmp_path).run_paired()


# -----------------------------------------------------------------------------
# ctx.location() and ctx.service()
#


def test_a_location_is_under_the_temp_dir(tmp_path):
    ctx = ctx_for(py_test(passes), tmp_path, test_env=resolve(py_test(passes), Made("r"), key="c"))
    assert ctx.location("tbl") == str(tmp_path / "tbl")


def test_ctx_service_is_the_runs_instance_of_a_service_behind_the_inputs(tmp_path):
    seen = {}

    def body(ctx):
        seen["attributes"] = ctx.service("store").attribute_values()
        with pytest.raises(UsageError, match=r"ctx.service\('other'\): this test reaches services \['store'\]"):
            ctx.service("other")

    assert go(tmp_path, [py_test(body, inputs=[StoreDef()])])[0] == 0
    assert seen["attributes"] == {"endpoint": "http://store"}


# -----------------------------------------------------------------------------
# What a value brings reaches DuckDB as preamble text
#


def test_the_test_environment_reaches_the_preamble_in_the_order_of_the_init_file(tmp_path):
    seen = []

    def body(ctx):
        seen.extend(ctx.client("duck").engine.live.preambles)

    value = matrix_value(
        "s3",
        requires=("httpfs",),
        settings={"threads": 2, "s3_region": "it's"},
        init_sql="CREATE SECRET (ENDPOINT 'http://store')",
    )
    code, _, _, _ = go(tmp_path, [py_test(body, {"k": value})])
    assert code == 0
    assert seen == [
        "LOAD httpfs;\nSET threads = '2';\nSET s3_region = 'it''s';\nCREATE SECRET (ENDPOINT 'http://store');",
    ]


def test_what_a_run_writes_under_a_storage_is_under_its_run_token_and_a_killed_run_is_swept(tmp_path):
    store = StorageDef("scratch", str(tmp_path / "store"))

    def body(ctx):
        os.makedirs(ctx.location("t"))

    tests = [sql_test("test/writes.test", temp=store), py_test(body, temp=store)]
    code, run_dir, _, _ = go(tmp_path, tests, keep=True)
    assert code == 0
    run_root = tmp_path / "store" / "temp" / run_dir.name
    ((batch_dir,),) = [list((run_dir / "batches").iterdir())]
    config = json.loads((batch_dir / "config.json").read_text())
    assert (config["temp_dir_root"], config["run_id"]) == (str(run_root), batch_dir.name)
    assert (run_root / batch_dir.name / "test_writes.test" / "out").is_file()
    assert (run_root / tests[1].dir_name / "t").is_dir()
    other = tmp_path / "store" / "temp" / "another-run" / "b001"
    other.mkdir(parents=True)

    project = SimpleNamespace(storages={store.name: store}, services={})
    summary = phases.cleanup(str(run_dir), project=project, event_lines=[{"event": "run_start"}])
    assert sorted(summary["swept"]) == sorted(f"{run_root}/{name}" for name in (batch_dir.name, tests[1].dir_name))
    assert not run_root.exists() and other.is_dir()


def test_a_test_whose_environment_differs_from_the_runs_gets_a_shell_of_its_own(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        made.append(FakeShell(binary, home=home, env=env))
        return made[-1]

    def body(ctx):
        ctx.client("duck").oks("SELECT 1")

    value = matrix_value("no-key", process_env={"AWS_PROFILE": "ducktest", "AWS_ACCESS_KEY_ID": None})
    tests = [py_test(body, path="test/plain.py"), py_test(body, {"auth": value}, path="test/env.py")]
    assert go(tmp_path, tests, shell_factory=factory, workers=1)[0] == 0
    plain, own = sorted(made, key=lambda s: "AWS_PROFILE" in s.env)
    assert ("AWS_PROFILE" in plain.env, own.env["AWS_PROFILE"]) == (False, "ducktest")
    assert "AWS_ACCESS_KEY_ID" not in own.env


def test_a_new_client_whose_preamble_fails_is_a_setup_error(tmp_path):
    made = []

    def factory(binary, **kw):
        made.append(FakeShell(binary, preamble_fails=bool(made), **kw))
        return made[-1]

    def body(ctx):
        ctx.client("duck", new=True)

    value = matrix_value("s3", init_sql="CREATE SECRET s")
    _, _, report, _ = go(tmp_path, [py_test(body, {"k": value})], shell_factory=factory)
    assert report["tests"][0]["outcome"] == "error"
    assert "[duck/duck-shell] the preamble failed: Catalog Error" in report["tests"][0]["problems"][0]


def test_a_new_client_is_a_warm_shell_of_its_own_closed_when_the_test_ends(tmp_path):
    made = []

    def factory(binary, *, home=None, env=None):
        shell = FakeShell(binary, home=home, env=env)
        shell.close = lambda: made_closed.append(shell)
        made.append(shell)
        return shell

    made_closed = []

    def body(ctx):
        shared = ctx.client("duck")
        first = ctx.client("duck", new=True)
        second = ctx.client("duck", new=True)
        first.oks("SELECT 1")
        second.oks("SELECT 2")
        shared.oks("SELECT 3")

    value = matrix_value("s3", requires=("httpfs",), process_env={"AWS_PROFILE": "ducktest"})
    code, _, _, _ = go(tmp_path, [py_test(body, {"k": value})], shell_factory=factory)
    assert code == 0
    run_shell, first, second = made  # the run's shell, then one for each new client
    assert [first.statements, second.statements, run_shell.statements] == [["SELECT 1"], ["SELECT 2"], ["SELECT 3"]]
    assert [first.preambles, second.preambles] == [run_shell.preambles] * 2 == [["LOAD httpfs;"]] * 2
    assert [first.home, second.home] == [run_shell.home] * 2
    assert first.env["AWS_PROFILE"] == second.env["AWS_PROFILE"] == "ducktest"
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
        seen.append((os.path.basename(ctx.temp_dir), os.listdir(ctx.temp_dir)))
        open(os.path.join(ctx.temp_dir, "left-behind"), "w").close()
        assert len(seen) == 2

    test = py_test(flaky, retries=2)
    code, run_dir, report, _ = go(tmp_path, [test])
    assert code == 0
    assert seen == [(test.dir_name, []), (test.dir_name + ".attempt2", [])]
    assert (report["tests"][0]["attempts"], report["tests"][0]["note"]) == (2, "passed on attempt 2 of 3")
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


def test_duckdb_gets_a_home_under_the_run_dir(tmp_path):
    seen = {}

    def body(ctx):
        seen["home"] = ctx.client("duck").engine.live.home

    _, run_dir, _, _ = go(tmp_path, [py_test(body)])
    assert seen == {"home": str(run_dir / "home")}


def test_the_timeout_of_the_engine_reaches_each_statement_of_a_new_client_too(tmp_path):
    seen = []

    def body(ctx):
        ctx.client("duck").oks("SELECT 1")
        new = ctx.client("duck", new=True)
        new.oks("SELECT 2")
        seen.extend(ctx.client("duck").engine.live.timeouts + new.engine.live.timeouts)

    assert go(tmp_path, [py_test(body)], engines=[DuckEngineDef("duck", statement_timeout=7)])[0] == 0
    assert seen == [7, 7]


def test_the_scan_record_names_every_engine_a_test_runs_on_and_its_version(tmp_path):
    project = LoadedProject(str(tmp_path), {"fake": FakeEngine().engine()}, {}, {}, {})
    record = py_test(passes, engines=["fake"]).record(project)
    assert record["preconditions"]["engines"] == [
        {"name": "duck", "version": "duck"},
        {"name": "fake", "version": "fake:1"},
    ]
    assert sql_test("a.test").record(project)["preconditions"]["engines"] == [{"name": "duck", "version": "duck"}]


def test_select_explain_shows_an_engine_named_on_test(tmp_path, capsys, monkeypatch):
    (tmp_path / "ducktest_conf.py").write_text(
        "from ducktest import Project\nfrom ducktest.engines.duckdb import DuckEngineDef\nproject = Project(engines=[DuckEngineDef('duck')])\n"
    )
    (tmp_path / "t.py").write_text("from ducktest import test\n\n\n@test(engines=['duck'])\ndef t(ctx):\n    pass\n")
    monkeypatch.chdir(tmp_path)
    assert main(["select", "--explain"]) == 0
    assert "engines=['duck']  <- @test" in capsys.readouterr().out


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


# -----------------------------------------------------------------------------
# The default storage
#


def test_a_python_body_that_binds_no_storage_is_given_the_roots_a_test_file_is(tmp_path):
    seen = {}

    def body(ctx):
        seen["names"] = {name: ctx.values[name] for name in ("DATA_DIR", "TEMP_DIR", "TEMP_DIR_ROOT")}

    test = py_test(body)
    code, run_dir, _, _ = go(tmp_path, [test, sql_test("x.test")], keep=True)
    assert code == 0
    data, own = f"{tmp_path}/data", f"{run_dir}/tests/{test.dir_name}"
    assert seen["names"] == {"DATA_DIR": data, "TEMP_DIR": own, "TEMP_DIR_ROOT": str(run_dir)}
    (config,) = run_dir.glob("batches/*/config.json")
    given = json.loads(config.read_text())
    assert (given["data_dir"], given["temp_dir_root"]) == (data, str(run_dir))


@pytest.mark.parametrize(
    "slots, data_dir, temp_dir_root",
    [
        ({"temp": "store"}, "{tmp}/data", "{tmp}/store/temp/{run}"),
        ({"temp": "store.at"}, "{tmp}/data", "{tmp}/store/ducktest/{run}"),
        ({"data": "store"}, "{tmp}/store", "{tmp}/runs/{run}"),
        ({"data": "store.at"}, "{tmp}/store/fixtures", "{tmp}/runs/{run}"),
        ({"storage": "store"}, "{tmp}/data", "{tmp}/runs/{run}"),
        ({"data": "store.at", "temp": "store.at"}, "{tmp}/store/fixtures", "{tmp}/store/ducktest/{run}"),
    ],
)
def test_data_and_temp_each_move_their_own_root_and_storage_moves_neither(tmp_path, slots, data_dir, temp_dir_root):
    seen = {}

    def body(ctx):
        seen.update({n: ctx.values[n] for n in ("data_dir", "temp_dir", "temp_dir_root")})
        open(os.path.join(ctx.values.temp_dir, "f"), "w").close()

    store = StorageDef("store", str(tmp_path / "store"))
    places = {"store": store, "store.at": None}
    given = {
        slot: store.at("fixtures" if slot == "data" else "ducktest") if v == "store.at" else places[v]
        for slot, v in slots.items()
    }
    test = py_test(body, **given)
    code, run_dir, _, _ = go(tmp_path, [test])
    assert code == 0
    fill = {"tmp": tmp_path, "run": run_dir.name}
    assert seen["data_dir"] == data_dir.format(**fill)
    assert seen["temp_dir_root"] == temp_dir_root.format(**fill)
    own = "tests/" if seen["temp_dir_root"] == str(run_dir) else ""
    assert seen["temp_dir"] == f"{seen['temp_dir_root']}/{own}{test.dir_name}"
    if "temp" in slots:
        assert not os.path.exists(seen["temp_dir_root"]), "a green run leaves nothing in the place it wrote"


# -----------------------------------------------------------------------------
# A Python body that skips
#


def _not_built():
    """A helper with no ctx, as a repo's own module would hold."""
    from ducktest import skip

    skip("duckdb CLI not built")


def test_a_body_that_skips_is_skipped_with_its_reason_and_keeps_its_statement_log(tmp_path):
    from ducktest.reports import Report, junit

    def skips(ctx):
        ctx.client("duck").oks("SELECT 1")
        ctx.skip("this build has no retry")

    code, run_dir, report, text = go(tmp_path, [py_test(skips), py_test(passes, path="test/p.py")])
    test = result_of(report, "skips")
    assert (code, test["outcome"], test["reason"]) == (0, "skipped", "this build has no retry")
    assert "failure" not in test and "kept" not in test
    assert [s["sql"] for s in test["statements"]] == ["SELECT 1"]
    assert "1 skipped:\n     1  this build has no retry" in text
    out = io.StringIO()
    junit.render(Report.of(read_events(str(run_dir))), out)
    assert '<skipped message="this build has no retry" />' in out.getvalue()


def test_skip_from_a_helper_with_no_ctx_skips_the_body_and_removes_its_dir(tmp_path):
    def uses_helper(ctx):
        writes(ctx)
        _not_built()

    code, run_dir, report, _ = go(tmp_path, [py_test(uses_helper)])
    assert (code, report["tests"][0]["outcome"], report["tests"][0]["reason"]) == (1, "skipped", "duckdb CLI not built")
    assert list((run_dir / "tests").iterdir()) == []


def test_a_skip_is_not_swallowed_by_a_body_s_own_except_exception(tmp_path):
    def guarded(ctx):
        try:
            ctx.skip("not here")
        except Exception:
            pass

    test = py_test(guarded)
    assert _run_body(test, ctx_for(test, tmp_path))[:2] == ("skipped", ["not here"])


def test_a_skip_after_a_failed_check_is_a_failure(tmp_path):
    def body(ctx):
        ctx.client("fake").expects("SELECT 1", [2], on_fail="continue")
        ctx.skip("too late")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "failed" and len(problems) == 1 and "wrong result" in problems[0]


@pytest.mark.parametrize("teardown_fails", [False, True])
def test_teardowns_run_after_a_skip_and_one_that_fails_is_an_error(tmp_path, teardown_fails):
    engine = FakeEngine()

    def body(ctx):
        ctx.client("fake").teardown("DROP boom" if teardown_fails else "DROP t")
        ctx.skip("no feature")

    test = py_test(body, engines=["fake"])
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path, engine=engine))
    assert outcome == ("error" if teardown_fails else "skipped")
    assert engine.statements[-1] == ("DROP boom" if teardown_fails else "DROP t")
    assert problems[-1].startswith("teardown failed") if teardown_fails else problems == ["no feature"]


def test_a_skipped_body_is_not_retried(tmp_path):
    seen = []

    def skips(ctx):
        seen.append(1)
        ctx.skip("never")

    _, _, report, _ = go(tmp_path, [py_test(skips, retries=2)])
    assert (seen, report["tests"][0]["outcome"], "attempts" in report["tests"][0]) == ([1], "skipped", False)


def test_a_skip_with_no_reason_is_a_usage_error_of_the_test(tmp_path):
    def body(ctx):
        ctx.skip("  ")

    test = py_test(body)
    outcome, problems, _ = _run_body(test, ctx_for(test, tmp_path))
    assert outcome == "error" and "give the reason" in problems[0]
