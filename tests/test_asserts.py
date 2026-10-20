"""What a plain `assert` in a ducktest body says when it fails."""

import importlib.util
import io
import sys
import textwrap
import traceback

import pytest
import yaml

from ducktest import asserts
from ducktest.binary import UsageError
from ducktest.events import TEST_END
from ducktest.events import read as read_events
from ducktest.run import run
from ducktest.scan import scan


def end_events(run_dir):
    """The `test_end` lines of a run: one per test, whatever else the stream holds."""
    return [e for e in read_events(str(run_dir)) if e["event"] == TEST_END]


def load(tmp_path, source, name="probe"):
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source))
    spec = importlib.util.spec_from_file_location(name, str(path), loader=asserts.loader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def failure(fn, *args):
    with pytest.raises(AssertionError) as raised:
        fn(*args)
    return str(raised.value)


def test_a_wrong_list_of_rows_prints_both_sides_and_a_diff(tmp_path):
    module = load(
        tmp_path,
        """
        def check(ids):
            assert ids == list(range(1, len(ids) + 1))
        """,
    )
    assert failure(module.check, [1, 2, 4]) == textwrap.dedent(
        """\
        assert ids == list(range(1, len(ids) + 1))
          left:
            1
            2
            4
          right:
            1
            2
            3
          diff, - left + right:
            @@ -1,3 +1,3 @@
             1
             2
            -4
            +3"""
    )


def test_rows_of_several_values_read_like_a_failed_expects(tmp_path):
    module = load(
        tmp_path,
        """
        def check(rows):
            assert rows == [(1, "a"), (2, "b")]
        """,
    )
    printed = failure(module.check, [(1, "a"), (2, None)])
    assert "2\tNULL" in printed and "2\tb" in printed


def test_two_short_values_take_one_line_each(tmp_path):
    module = load(tmp_path, "def check(n):\n    assert n == 3\n")
    assert failure(module.check, 4) == "assert n == 3\n  left:  4\n  right: 3"


def test_a_message_comes_before_the_assert(tmp_path):
    module = load(tmp_path, 'def check(n):\n    assert n == 3, f"reader {n} saw it"\n')
    assert failure(module.check, 4).splitlines()[0] == "reader 4 saw it"


@pytest.mark.parametrize(
    "test, value, expected",
    [
        ("assert value", "", "assert value\n  value: ''"),
        ("assert not value", "x", "assert not value\n  value: 'x'"),
        ("assert value in (1, 2)", 3, "assert value in (1, 2)\n  left:  3\n  right: (1, 2)"),
        ("assert value < 2", 7, "assert value < 2\n  left:  7\n  right: 2"),
        ("assert value.upper() == 'A'", "b", "assert value.upper() == 'A'\n  left:  'B'\n  right: 'A'"),
    ],
)
def test_every_shape_of_assert_says_what_the_value_was(tmp_path, test, value, expected):
    module = load(tmp_path, f"def check(value):\n    {test}\n")
    assert failure(module.check, value) == expected


def test_only_the_comparison_that_failed_shows_a_diff(tmp_path):
    module = load(tmp_path, "def check(a, b):\n    assert a in b\n")
    assert "diff" not in failure(module.check, 9, [1, 2, 3])


def test_a_side_is_evaluated_once(tmp_path):
    module = load(
        tmp_path,
        """
        calls = []
        def grow():
            calls.append(1)
            return len(calls)
        def check():
            assert grow() == 99
        """,
    )
    failure(module.check)
    assert module.calls == [1]


def test_the_traceback_points_at_the_assert_line(tmp_path):
    module = load(tmp_path, "def check():\n    x = 1\n\n\n    assert x == 2\n")
    with pytest.raises(AssertionError) as raised:
        module.check()
    assert traceback.extract_tb(raised.value.__traceback__)[-1].lineno == 5


def test_a_helper_beside_the_tests_gets_the_same_text(tmp_path, monkeypatch):
    (tmp_path / "beside_the_tests.py").write_text("def check(name):\n    assert name == 'duck'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    asserts.rewrite_under(str(tmp_path))
    module = load(tmp_path, "from beside_the_tests import check\n", name="uses_helper")
    assert failure(module.check, "goose") == "assert name == 'duck'\n  left:  'goose'\n  right: 'duck'"


def test_nothing_is_written_beside_the_test_file(tmp_path):
    load(tmp_path, "def check():\n    assert True\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["probe.py"]


class NoShell:
    """The run's DuckDB shell, for a body that never asks for one."""

    def __init__(self, binary, *, home=None, env=None):
        pass

    begin_test = close = lambda self: None


def test_the_failure_text_reaches_the_terminal_the_report_and_the_events(tmp_path):
    root = tmp_path / "test"
    root.mkdir()
    (root / "ducktest_conf.py").write_text("from ducktest import Project\nproject = Project()\n")
    (root / "body.py").write_text(
        textwrap.dedent(
            """
            from ducktest import test

            @test()
            def ids_are_gapless(ctx):
                ids = [1, 2, 4]
                assert ids == list(range(1, len(ids) + 1))
            """
        )
    )
    tests, project = scan(str(root), working_dir=str(tmp_path))
    printed = io.StringIO()
    code = run(
        tests,
        project,
        duckdb_bin="no-duckdb",
        working_dir=str(tmp_path),
        temp_root=str(tmp_path / "runs"),
        out=printed,
        shell_factory=NoShell,
    )
    assert code == 1
    (run_dir,) = (tmp_path / "runs").iterdir()
    report = yaml.safe_load((run_dir / "05-report.yaml").read_text())
    (problem,) = report["tests"][0]["problems"]
    assert "assert ids == list(range(1, len(ids) + 1))" in problem
    assert "diff, - left + right" in problem
    assert problem.endswith("\n  at body.py:7")  # as the test id names it: from the project root
    # the terminal shows the message and the place, laid out; the event keeps the text as it was
    assert "assert ids == list(range(1, len(ids) + 1))" in printed.getvalue()
    assert "body.py:7" in printed.getvalue()
    (event,) = end_events(run_dir)
    assert event["problems"] == [problem]
    assert event["failure"]["where"] == "body.py:7"


# -----------------------------------------------------------------------------
# SQL written as an f-string
#


def test_an_f_string_as_the_sql_of_a_verb_is_refused_at_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.py").write_text(
        "from ducktest import test\n\n\n@test()\ndef t(ctx):\n    n = 1\n"
        '    ctx.client("duckdb").oks(f"SELECT {n}")\n'
    )
    with pytest.raises(UsageError) as e:
        scan(str(tmp_path), working_dir=str(tmp_path))
    assert str(e.value) == (
        "t.py:7: SQL given to oks() is an f-string; write the name as {n} and set it with ctx.values.set(n=...), "
        "a rule's values=, or oks(..., n=...). Templates are filled per engine."
    )


def test_sql_built_on_its_own_line_and_passed_by_name_is_not_refused(tmp_path):
    (tmp_path / "t.py").write_text(
        "from ducktest import test\n\n\n@test()\ndef t(ctx):\n    n = 1\n"
        '    sql = f"SELECT {n}"\n    ctx.client("duckdb").oks(sql)\n'
    )
    tests, _ = scan(str(tmp_path), working_dir=str(tmp_path))
    assert [t.name for t in tests] == ["t.py::t"]
