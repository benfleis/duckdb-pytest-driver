"""ducktest5's run loop, with no DuckDB and no Spark: a fake engine, a fake service, a fake DuckDB
session and the fake unittest binary."""

import io
import json
import os

import pytest
import yaml

from ducktest5.binary import UsageError
from ducktest5.client import SqlError
from ducktest5.conf import Project
from ducktest5.ctx import Ctx
from ducktest5.declare import Engine, Service, Variant
from ducktest5.model import PAIRED, PYTHON, SQLLOGIC, Case, TestDef
from ducktest5.provision import EXISTING, Provisioned
from ducktest5.run import _run_body, run

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")


class FakeEngine:
    """An engine that answers every statement with one row, and records what it was asked."""

    def __init__(self, *, boots=True):
        self.boots, self.calls, self.statements = boots, [], []

    def engine(self, id="fake", uses=()):
        return Engine(id, "fake:1", self._boot, uses=uses)

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

        return Service(
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

    def __init__(self, binary=None, *, home=None, preamble_fails=False):
        self.preambles, self.statements, self.preamble_fails, self.home = [], [], preamble_fails, home

    def begin_test(self):
        self.preambles = []

    def add_preamble(self, preamble):
        if self.preamble_fails:
            raise SqlError("Catalog Error: no such secret type")
        self.preambles.append(preamble)

    def run(self, statements):
        self.statements += statements
        return ["v"], [(1,)]

    restart = respawn = close = lambda self: None


def sql_case(path, axis_values=None, **fields):
    name = os.path.splitext(os.path.basename(path))[0]
    return Case(TestDef(path=path, name=name, type=SQLLOGIC, body_file=path, **fields), None, axis_values or {})


def py_case(body, axis_values=None, *, path="test/body.py", body_file=None, **fields):
    test = TestDef(
        path=path, name=body.__name__, type=PAIRED if body_file else PYTHON, body=body, body_file=body_file, **fields
    )
    return Case(test, None, axis_values or {})


def go(tmp_path, cases, *, engines=(), services=(), unittest_bin=FAKE, **kwargs):
    """Returns (exit code, run dir, report, printed text)."""
    project = Project(str(tmp_path), {e.id: e for e in engines}, {s.id: s for s in services}, {}, {}, ())
    out = io.StringIO()
    kwargs.setdefault("session_factory", FakeSession)
    code = run(
        cases,
        project,
        duckdb_bin="no-duckdb",
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
    return {c["id"].split("::")[-1]: c["outcome"] for c in report["cases"]}


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
    assert go(tmp_path, [sql_case(n) for n in names])[0] == code


def test_a_blocked_case_fails_the_run(tmp_path):
    store = FakeStore(up=False)
    code, _, report, _ = go(
        tmp_path, [py_case(passes), py_case(passes, services=["store"], path="test/s.py")], services=[store.service()]
    )
    assert code == 1
    assert [c["outcome"] for c in report["cases"]] == ["blocked", "passed"]


# -----------------------------------------------------------------------------
# Batches
#


def batch_of(report):
    return {c["id"].split("::")[-1]: c["batch"] for c in report["cases"]}


def test_cases_of_one_file_never_share_a_batch(tmp_path):
    axis = {"a": Variant("a", tags=("x",)), "b": Variant("b", tags=("y",))}
    cases = [sql_case("t.test", {"k": axis["a"]}), sql_case("t.test", {"k": axis["b"]}), sql_case("u.test")]
    code, run_dir, report, _ = go(tmp_path, cases, keep=True)
    assert code == 0
    batches = batch_of(report)
    assert batches["t[k=a]"] != batches["t[k=b]"]
    assert batches["u"] == batches["t[k=a]"]  # same values and settings: it rides along
    assert (run_dir / batches["t[k=a]"] / "tests.txt").read_text() == "t.test\nu.test\n"
    assert (run_dir / batches["t[k=b]"] / "tests.txt").read_text() == "t.test\n"


def test_cases_with_other_values_or_settings_go_in_other_batches(tmp_path):
    cases = [
        sql_case("a.test"),
        sql_case("b.test"),
        sql_case("c_env.test", values={"catalog": "duck"}),
        sql_case("d.test", settings={"threads": 2}),
    ]
    _, run_dir, report, _ = go(tmp_path, cases, keep=True)
    batches = batch_of(report)
    assert batches["a"] == batches["b"]
    assert len({batches["a"], batches["c_env"], batches["d"]}) == 3
    config = json.loads((run_dir / batches["c_env"] / "config.json").read_text())
    assert config["test_env"] == [{"env_name": "CATALOG", "env_value": "duck"}]
    config = json.loads((run_dir / batches["d"] / "config.json").read_text())
    assert config["settings"] == [{"name": "threads", "value": "2"}]


def test_a_batch_holds_at_most_batch_size_files_and_a_slow_file_runs_alone(tmp_path):
    cases = [sql_case(f"t{n}.test") for n in range(3)] + [sql_case("s.test_slow", tags=["slow"])]
    _, _, report, _ = go(tmp_path, cases, batch_size=2)
    batches = batch_of(report)
    assert sorted(list(batches.values()).count(b) for b in set(batches.values())) == [1, 1, 2]
    assert list(batches.values()).count(batches["s"]) == 1


def test_a_case_with_a_service_runs_alone_and_its_failure_is_not_run_a_second_time(tmp_path, flaky_state):
    store = FakeStore()
    cases = [sql_case("a.test"), sql_case("b.test", services=["store"]), sql_case("c_flaky2.test", services=["store"])]
    _, _, report, _ = go(tmp_path, cases, services=[store.service()])
    assert len(set(batch_of(report).values())) == 3
    assert outcomes(report)["c_flaky2"] == "failed"
    assert (tmp_path / "state" / "c_flaky2.test.runs").read_text() == "1"


def test_a_variant_init_sql_is_the_batch_init_file(tmp_path):
    store = FakeStore()
    variant = Variant("s3", services=("store",), init_sql=lambda blocks: f"ATTACH '{blocks['store']['endpoint']}'")
    _, run_dir, report, _ = go(tmp_path, [sql_case("a.test", {"k": variant})], services=[store.service()], keep=True)
    init = run_dir / report["cases"][0]["batch"] / "init.sqllogic"
    assert init.read_text() == "statement ok\nATTACH 'http://store';\n"


# -----------------------------------------------------------------------------
# What does not come up blocks its cases, and only those
#


def test_a_service_that_is_down_blocks_its_cases_and_their_engine_is_not_booted(tmp_path):
    store, engine = FakeStore(up=False), FakeEngine()
    cases = [py_case(passes, services=["store"], engines=["fake"]), py_case(passes, path="test/free.py")]
    code, run_dir, report, text = go(tmp_path, cases, engines=[engine.engine()], services=[store.service()])
    assert [c["outcome"] for c in report["cases"]] == ["blocked", "passed"]
    assert report["cases"][0]["blocked_by"] == ["store"]
    assert "does not answer" in report["not_provisioned"]["store"]
    assert engine.calls == []
    assert "'store' did not come up: 1 cases blocked" in text


def test_an_engine_that_does_not_boot_blocks_its_cases(tmp_path):
    engine = FakeEngine(boots=False)
    cases = [py_case(passes, engines=["fake"]), sql_case("a.test")]
    _, _, report, _ = go(tmp_path, cases, engines=[engine.engine()])
    assert outcomes(report) == {"passes": "blocked", "a": "passed"}
    assert "no JVM" in report["not_provisioned"]["fake"]


def test_an_engine_is_told_about_the_services_it_uses_and_is_stopped(tmp_path):
    store, engine = FakeStore(), FakeEngine()
    case = py_case(passes, services=["store"], engines=["fake"])
    go(tmp_path, [case], engines=[engine.engine(uses=("store", "absent"))], services=[store.service()])
    assert engine.calls == ["boot", "use store", "begin_test", "stop"]


def test_secrets_of_a_service_are_not_in_the_provision_record(tmp_path):
    _, run_dir, _, _ = go(tmp_path, [py_case(passes, services=["store"])], services=[FakeStore().service()])
    record = yaml.safe_load((run_dir / "03-provision.yaml").read_text())
    assert record == {"store": {"mode": EXISTING, "block": {"endpoint": "http://store", "secret_key": "***"}}}


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
def test_what_a_passed_case_wrote_is_removed_unless_kept(tmp_path, keep):
    store = FakeStore()
    code, run_dir, report, _ = go(
        tmp_path, [py_case(writes, services=["store"])], services=[store.service()], keep=keep
    )
    assert code == 0
    uri = f"fake://{run_dir.name}/{py_case(writes).dir_name}/tbl"
    if keep:
        assert store.cleaned == [] and len(list((run_dir / "cases").iterdir())) == 1
        assert report["cases"][0]["kept"][1:] == [uri]
    else:
        assert store.cleaned == [uri] and list((run_dir / "cases").iterdir()) == []
        assert "kept" not in report["cases"][0]


def test_what_a_failed_case_wrote_stays_and_is_listed(tmp_path):
    store = FakeStore()
    _, run_dir, report, text = go(tmp_path, [py_case(writes_and_fails, services=["store"])], services=[store.service()])
    kept = report["cases"][0]["kept"]
    assert store.cleaned == [] and os.path.isfile(os.path.join(kept[0], "f")) and kept[1].startswith("fake://")
    assert "kept: " + kept[0] in text and "again: ducktest5 run --id" in text


@pytest.mark.parametrize("keep", [False, True])
def test_a_batch_dir_stays_only_when_kept_or_not_all_passed(tmp_path, keep):
    _, run_dir, _, _ = go(tmp_path, [sql_case("a.test")], keep=keep)
    assert (run_dir / "batches" / "b001").exists() == keep


def test_a_batch_dir_with_a_failure_stays(tmp_path):
    _, run_dir, report, _ = go(tmp_path, [sql_case("a.test"), sql_case("b_fail.test")])
    assert (run_dir / "batches" / "b001" / "output.txt").exists()
    assert "Wrong result in query" in report["cases"][1]["problems"][0]


# -----------------------------------------------------------------------------
# Whatever goes wrong outside a body is an ERROR of the cases it hit
#


def test_a_clean_that_raises_is_an_error_and_the_run_goes_on(tmp_path):
    store = FakeStore(clean_raises=True)
    cases = [py_case(writes, services=["store"]), py_case(passes, path="test/next.py")]
    code, _, report, _ = go(tmp_path, cases, services=[store.service()])
    assert code == 1
    assert outcomes(report) == {"writes": "error", "passes": "passed"}
    first = report["cases"][0]
    assert "could not be removed: RuntimeError: access denied" in first["problems"][0]
    assert first["kept"] == [first["problems"][0].split(" at ")[1].split(" could")[0]]


def test_a_batch_that_cannot_start_is_an_error_of_its_cases_and_python_cases_still_run(tmp_path):
    cases = [sql_case("a.test"), sql_case("b.test"), py_case(passes)]
    code, _, report, _ = go(tmp_path, cases, unittest_bin=str(tmp_path / "no-such-binary"))
    assert code == 1
    assert outcomes(report) == {"a": "error", "b": "error", "passes": "passed"}
    assert "the batch could not run: FileNotFoundError" in report["cases"][0]["problems"][0]


def test_a_case_dir_that_cannot_be_made_is_an_error_of_that_case(tmp_path, monkeypatch):
    case = py_case(passes)
    real = os.makedirs
    monkeypatch.setattr(
        os,
        "makedirs",
        lambda p, **kw: (_ for _ in ()).throw(OSError("disk full")) if case.dir_name in p else real(p, **kw),
    )
    _, _, report, _ = go(tmp_path, [case, py_case(passes, path="test/other.py")])
    assert [c["outcome"] for c in report["cases"]] == ["error", "passed"]
    assert "could not be set up: OSError: disk full" in report["cases"][0]["problems"][0]


def test_the_report_is_written_and_services_are_released_when_the_run_raises(tmp_path):
    engine = FakeEngine()
    with pytest.raises(UsageError, match="no unittest binary"):
        run(
            [sql_case("a.test"), py_case(passes, engines=["fake"])],
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


def test_each_case_has_one_event_line(tmp_path):
    variant = Variant("oss")
    _, run_dir, _, _ = go(tmp_path, [sql_case("a.test", {"backend": variant}), py_case(fails)])
    events = [json.loads(line) for line in (run_dir / "04-events.jsonl").read_text().splitlines()]
    assert [(e["id"], e["outcome"], e["runner"], e["variants"]) for e in events] == [
        ("a.test::a[backend=oss]", "passed", "unittest", {"backend": "oss"}),
        ("test/body.py::fails", "failed", "python", {}),
    ]


# -----------------------------------------------------------------------------
# A body: checks, abort, teardowns
#


def ctx_for(case, tmp_path, *, engine=None, session=None, services=None, **kwargs):
    return Ctx(
        case,
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

    case = py_case(body, engines=["fake"])
    outcome, problems = _run_body(case, ctx_for(case, tmp_path))
    assert outcome == "failed"
    assert ["2" in problems[0], "3" in problems[1], "then the body broke" in problems[2]] == [True] * 3
    assert f"at {__file__}:" in problems[0]


def test_failed_checks_alone_fail_the_case(tmp_path):
    def body(ctx):
        ctx.engine("fake").expects("SELECT 1", [2], on_fail="continue")

    case = py_case(body, engines=["fake"])
    assert _run_body(case, ctx_for(case, tmp_path))[0] == "failed"


def test_a_failed_check_stays_a_failure_when_a_setup_fails_after_it(tmp_path):
    def body(ctx):
        c = ctx.engine("fake")
        c.expects("SELECT 1", [2], on_fail="continue")
        c.setup("SELECT boom")

    case = py_case(body, engines=["fake"])
    outcome, problems = _run_body(case, ctx_for(case, tmp_path))
    assert outcome == "failed"
    assert "wrong result" in problems[0] and problems[1].startswith("setup failed")


def test_a_failed_setup_is_an_error_not_a_failure(tmp_path):
    def body(ctx):
        ctx.engine("fake").setup("SELECT boom")

    case = py_case(body, engines=["fake"])
    outcome, problems = _run_body(case, ctx_for(case, tmp_path))
    assert outcome == "error" and problems[0].startswith("setup failed")


@pytest.mark.parametrize("aborts", [False, True])
def test_teardowns_run_in_reverse_order_also_after_an_abort(tmp_path, aborts):
    engine = FakeEngine()

    def body(ctx):
        c = ctx.engine("fake")
        c.teardown("DROP first")
        c.teardown("DROP second")
        c.oks("SELECT boom" if aborts else "SELECT 1")

    case = py_case(body, engines=["fake"])
    outcome, _ = _run_body(case, ctx_for(case, tmp_path, engine=engine))
    assert outcome == ("failed" if aborts else "passed")
    assert engine.statements[-2:] == ["DROP second", "DROP first"]


def test_a_teardown_that_fails_is_an_error_and_the_others_still_run(tmp_path):
    engine = FakeEngine()

    def body(ctx):
        c = ctx.engine("fake")
        c.teardown("DROP last")
        c.teardown("DROP boom")

    case = py_case(body, engines=["fake"])
    outcome, problems = _run_body(case, ctx_for(case, tmp_path, engine=engine))
    assert outcome == "error" and problems[0].startswith("teardown failed")
    assert engine.statements[-1] == "DROP last"


def test_an_engine_the_test_did_not_declare_is_refused(tmp_path):
    ctx = ctx_for(py_case(passes), tmp_path)
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
    case = py_case(runs_its_file, body_file=body_file, values={"catalog": "duck"})
    _, run_dir, report, _ = go(tmp_path, [case], keep=True)
    assert report["cases"][0]["outcome"] == outcome
    if text:
        assert text in report["cases"][0]["problems"][0]
    else:
        output = (run_dir / "cases" / case.dir_name / "run_test" / "output.txt").read_text()
        assert 'ENV {"CATALOG": "duck", "EXTRA": "x"}' in output


def test_run_test_in_a_body_with_no_file_says_what_it_is_for(tmp_path):
    with pytest.raises(RuntimeError, match="a .test file of the same stem"):
        ctx_for(py_case(passes), tmp_path).run_test()


# -----------------------------------------------------------------------------
# ctx.location() and ctx.service()
#


def provisioned_store():
    service = FakeStore().service()
    return {"store": Provisioned(service, service.block(), EXISTING)}


def test_a_location_is_under_the_temp_dir_unless_a_service_is_named(tmp_path):
    case = py_case(passes, services=["store"])
    ctx = ctx_for(case, tmp_path, services=provisioned_store(), remote_parts=("run1", "case1"))
    assert ctx.location("tbl") == str(tmp_path / "tbl")
    assert ctx.remote_locations == []
    assert ctx.location("tbl", on="store") == "fake://run1/case1/tbl"
    assert ctx.remote_locations == [("store", "fake://run1/case1/tbl")]
    assert ctx.service("store")["endpoint"] == "http://store"


def test_a_variant_can_put_every_location_on_a_service(tmp_path):
    variant = Variant("s3", services=("store",), location_on="store")
    ctx = ctx_for(py_case(passes, {"k": variant}), tmp_path, services=provisioned_store(), remote_parts=("r", "c"))
    assert ctx.location("tbl") == "fake://r/c/tbl"


def test_a_service_the_test_did_not_declare_is_refused(tmp_path):
    ctx = ctx_for(py_case(passes), tmp_path, services=provisioned_store())
    with pytest.raises(KeyError, match="not declared by this test"):
        ctx.location("tbl", on="store")


# -----------------------------------------------------------------------------
# What a variant brings reaches DuckDB as preamble text
#


def test_variant_init_sql_and_settings_come_before_the_bodys_own_preamble(tmp_path):
    seen = []

    def body(ctx):
        ctx.duckdb("LOAD delta;")
        seen.extend(ctx._duckdb_session.preambles)

    variant = Variant(
        "s3",
        services=("store",),
        settings={"threads": 2, "s3_region": "it's"},
        init_sql=lambda blocks: f"CREATE SECRET (ENDPOINT '{blocks['store']['endpoint']}')",
    )
    code, _, _, _ = go(tmp_path, [py_case(body, {"k": variant})], services=[FakeStore().service()])
    assert code == 0
    assert seen == [
        "CREATE SECRET (ENDPOINT 'http://store');\nSET threads = '2';\nSET s3_region = 'it''s';\nLOAD delta;"
    ]


def test_a_preamble_that_fails_is_a_setup_error(tmp_path):
    def body(ctx):
        ctx.duckdb()

    _, _, report, _ = go(tmp_path, [py_case(body)], session_factory=lambda b, **kw: FakeSession(preamble_fails=True))
    assert report["cases"][0]["outcome"] == "error"
    assert "the preamble failed: Catalog Error" in report["cases"][0]["problems"][0]


def test_an_unknown_session_kind_is_refused(tmp_path):
    with pytest.raises(ValueError, match="'per_run' or 'per_call'"):
        ctx_for(py_case(passes), tmp_path).duckdb(session="per_test")


# -----------------------------------------------------------------------------
# Retries
#


def test_a_flaky_file_is_passed_with_its_attempts_in_the_event_line_and_its_output_stays(tmp_path, flaky_state):
    cases = [sql_case("a.test"), sql_case("b_flaky2.test", retries=1), sql_case("c_flaky2.test")]
    code, run_dir, report, text = go(tmp_path, cases)
    assert outcomes(report) == {"a": "passed", "b_flaky2": "passed", "c_flaky2": "failed"}
    events = [json.loads(line) for line in (run_dir / "04-events.jsonl").read_text().splitlines()]
    assert [e.get("attempts") for e in events] == [None, 2, None]
    assert "[passed on attempt 2 of 2]" in text


def test_the_output_of_a_failed_attempt_stays_when_a_later_attempt_passed(tmp_path, flaky_state):
    code, run_dir, _, _ = go(tmp_path, [sql_case("b_flaky2.test", retries=1)])
    assert code == 0
    assert "Wrong result in query" in (run_dir / "batches" / "b001" / "output.txt").read_text()


def test_a_python_body_with_retries_runs_again_in_a_directory_of_its_own(tmp_path):
    seen = []

    def flaky(ctx):
        seen.append((os.path.basename(ctx.temp_dir), os.listdir(ctx.temp_dir), ctx.location("t", on="store")))
        open(os.path.join(ctx.temp_dir, "left-behind"), "w").close()
        assert len(seen) == 2

    store = FakeStore()
    case = py_case(flaky, services=["store"], retries=2)
    code, run_dir, report, _ = go(tmp_path, [case], services=[store.service()])
    assert code == 0
    assert [(name, files) for name, files, _ in seen] == [(case.dir_name, []), (case.dir_name + ".attempt2", [])]
    assert (report["cases"][0]["attempts"], report["cases"][0]["note"]) == (2, "passed on attempt 2 of 3")
    assert store.cleaned == [uri for _, _, uri in seen] and len(set(store.cleaned)) == 2
    assert list((run_dir / "cases").iterdir()) == []


def test_a_python_body_that_never_passes_keeps_its_first_failure(tmp_path):
    seen = []

    def never(ctx):
        seen.append(1)
        assert False, f"attempt {len(seen)}"

    _, run_dir, report, _ = go(tmp_path, [py_case(never, retries=1)])
    case = report["cases"][0]
    assert (case["outcome"], case["attempts"], len(case["kept"])) == ("failed", 2, 2)
    assert "attempt 1" in case["problems"][0] and len(case["problems"]) == 1


def test_duckdb_gets_a_home_under_the_run_dir_for_both_kinds_of_session(tmp_path):
    seen = {}

    def body(ctx):
        seen["session"] = ctx._duckdb_session.home
        seen["per_call"] = ctx.duckdb(session="per_call")._run.__self__._home.path

    _, run_dir, _, _ = go(tmp_path, [py_case(body)])
    assert seen == {"session": str(run_dir / "home"), "per_call": str(run_dir / "home")}
