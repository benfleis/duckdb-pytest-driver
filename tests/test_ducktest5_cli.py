"""ducktest5's command line, on a small tree, with the fake unittest binary."""

import os
import textwrap

import pytest

from ducktest5.cli import main

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")

FILES = {
    "test/ducktestconf.py": """
        from ducktest5 import default, match
        EXCLUDE = ["generated"]
        RULES = [default(tags=["all"]), match("a/**", timeout=30)]
    """,
    "test/a/x.test": "",
    "test/a/y.test_slow": "",
    "test/b/z_fail.test": "",
    "test/generated/g.test": "",
}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    for name, body in FILES.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_args(*more):
    return ["run", "--duckdb-bin", FAKE, "--unittest-bin", FAKE, "--temp-dir-root", "runs", *more]


def test_select_says_what_runs_what_does_not_and_with_why_which_rule_gave_what(tree, capsys):
    assert main(["select", "test/a", "--why"]) == 0
    assert capsys.readouterr().out == textwrap.dedent(
        """\
        run   a/x.test::x
                tags=['all']  <- ducktestconf.py: default
                timeout=30  <- ducktestconf.py: match 'a/**'
        skip  a/y.test_slow::y  -- tag slow: runs only when asked for: --slow, --select tag=slow, or --id
        skip  b/z_fail.test::z_fail  -- outside the given paths

        1 of 3 cases
        """
    )


def test_select_without_why_prints_no_rules(tree, capsys):
    main(["select"])
    assert "<-" not in capsys.readouterr().out


def test_scan_prints_the_record_of_each_selected_case(tree, capsys):
    assert main(["scan", "test/a/x.test"]) == 0
    assert "id: a/x.test::x" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv, message",
    [
        (["select", "test/nowhere"], "path 'test/nowhere' holds no test"),
        (["select", "--select", "tag=typo"], "no scanned case has tag ['typo']"),
        (["select", "--id", "a/x.test::nope"], "--id: no such case"),
        (["select", "--set", "nightly"], "--set 'nightly': the project has sets []"),
        (["run", "--select", "not:tag=all"], "no case selected"),
        (["run", "test/a"], "duckdb shell not found"),
        (["run", "test/a", "--duckdb-bin", FAKE, "--unittest-bin", "absent"], "unittest binary not found"),
    ],
)
def test_a_usage_error_is_one_line_and_exit_code_2(tree, capsys, argv, message):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("ducktest5: ") and message in err and "Traceback" not in err


def test_an_unknown_option_is_refused_by_the_parser(tree):
    with pytest.raises(SystemExit) as refused:
        main(["select", "--keep"])  # a run option
    assert refused.value.code == 2


def test_run_gives_the_exit_code_of_the_run(tree, capsys):
    assert main(run_args("test/a", "--no-binary-check")) == 0
    assert main(run_args("test/b", "--no-binary-check")) == 1
    assert "FAILED   b/z_fail.test::z_fail" in capsys.readouterr().out


def test_an_id_names_the_same_case_from_any_directory(tree, monkeypatch, capsys):
    assert main(run_args("--id", "b/z_fail.test::z_fail", "--no-binary-check")) == 1
    monkeypatch.chdir(tree / "test" / "a")
    assert main(run_args("--id", "b/z_fail.test::z_fail", "--no-binary-check")) == 1
    assert capsys.readouterr().out.count("FAILED   b/z_fail.test::z_fail") == 2


def test_an_excluded_file_that_the_binary_knows_is_not_a_stale_build(tree, monkeypatch, capsys):
    known = tree / "known.txt"
    known.write_text("test/a/x.test\ntest/a/y.test_slow\ntest/b/z_fail.test\ntest/generated/g.test\n")
    monkeypatch.setenv("FAKE_UNITTEST_LIST", str(known))
    assert main(run_args("test/a")) == 0

    known.write_text("test/a/x.test\ntest/generated/g.test\ntest/gone.test\n")
    assert main(run_args("test/a")) == 2
    err = capsys.readouterr().err
    assert "test/gone.test" in err and "test/b/z_fail.test" in err and "generated" not in err
