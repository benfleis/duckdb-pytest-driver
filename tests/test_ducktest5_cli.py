"""ducktest5's command line, on a small tree, with the fake unittest binary."""

import json
import os
import sys
import textwrap

import pytest

from ducktest5.cli import main

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")
DEBUGGER = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_debugger.py")

FILES = {
    "test/ducktest_conf.py": """
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


def test_select_says_what_runs_what_does_not_and_with_explain_which_rule_gave_what(tree, capsys):
    # test/a is a directory PATH: it brings in the slow test under it too
    assert main(["select", "test/a", "--explain"]) == 0
    assert capsys.readouterr().out == textwrap.dedent(
        """\
        run   a/x.test::x
                tags=['all']  <- ducktest_conf.py: default
                timeout=30  <- ducktest_conf.py: match 'a/**'
        run   a/y.test_slow::y
                tags=['all']  <- ducktest_conf.py: default
                timeout=30  <- ducktest_conf.py: match 'a/**'
        skip  b/z_fail.test::z_fail  -- outside the given paths

        2 of 3 tests
        """
    )


def test_select_without_explain_prints_no_rules(tree, capsys):
    main(["select"])
    assert "<-" not in capsys.readouterr().out


def test_skip_removes_a_test_and_says_which_skip(tree, capsys):
    assert main(["select", "test/a", "--skip", "tag=all", "--explain"]) == 0
    assert capsys.readouterr().out == textwrap.dedent(
        """\
        skip  a/x.test::x  -- --skip tag=all
        skip  a/y.test_slow::y  -- --skip tag=all
        skip  b/z_fail.test::z_fail  -- outside the given paths

        0 of 3 tests
        """
    )


def test_scan_prints_the_record_of_each_selected_test(tree, capsys):
    assert main(["scan", "test/a/x.test"]) == 0
    assert "id: a/x.test::x" in capsys.readouterr().out


def test_scan_env_prints_what_each_test_is_given_and_where_it_came_from(tree, capsys):
    (tree / "test" / "ducktest_conf.py").write_text(
        textwrap.dedent("""
            from ducktest5 import Variant, default, match
            from ducktest5.resources import StorageDef
            from ducktest5.services.http import HttpServerDef
            STORAGES = [StorageDef("served", "{store.url}", inputs=[HttpServerDef("store")])]
            AXES = {"storage": [Variant(
                "s3",
                inputs=["served"],
                requires=["httpfs"],
                settings={"s3_region": "eu"},
                roots={"data_dir": "s3://bucket/data"},
                env={"AWS_PROFILE": "ducktest"},
                env_unset=["AWS_ACCESS_KEY_ID"],
            )]}
            EXCLUDE = ["generated"]
            RULES = [default(tags=["all"], values={"region": "eu"}), match("a/**", variants=["storage"])]
        """)
    )
    assert main(["scan", "test/a/x.test", "--env"]) == 0
    assert capsys.readouterr().out == textwrap.dedent(
        """\
        a/x.test::x[storage=s3]
          bind: storage=served  <- AXES storage=s3
          require: httpfs  <- AXES storage=s3
          set: s3_region=eu  <- AXES storage=s3
          values: storage=s3  <- AXES storage
          values: region=eu  <- ducktest_conf.py: default
          root: data_dir=s3://bucket/data  <- AXES storage=s3
          env: AWS_PROFILE=ducktest  <- AXES storage=s3
          env unset: AWS_ACCESS_KEY_ID  <- AXES storage=s3
          root: <from service store>  <- storage served
        """
    )


@pytest.mark.parametrize(
    "argv, message",
    [
        (["select", "test/nowhere"], "path 'test/nowhere' holds no test"),
        (["select", "--select", "tag=typo"], "no scanned test has tag ['typo']"),
        (["select", "--id", "a/x.test::nope"], "--id: no such test"),
        (["select", "--suite", "nightly"], "no suite ['nightly']; known suites: []"),
        (["select", "--skip", "not:tag=all"], "write !tag=all"),
        (["select", "--select", "tag=!all"], "write !tag=all"),
        (["run", "--select", "!tag=all"], "no test selected"),
        (["select", "--select", "test/nope"], "matches no scanned test"),
        (["select", "--select", "nope/outside"], "is outside the project root"),
        (["select", "--select", "a/b=c"], "a bare path can't contain '='"),
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


@pytest.mark.parametrize("option", ["--slow", "--why"])
def test_slow_and_why_are_gone(tree, option):
    with pytest.raises(SystemExit) as refused:
        main(["select", option])
    assert refused.value.code == 2


def test_run_gives_the_exit_code_of_the_run(tree, capsys):
    assert main(run_args("test/a", "--no-binary-check")) == 0
    assert main(run_args("test/b", "--no-binary-check")) == 1
    assert "FAILED   b/z_fail.test::z_fail" in capsys.readouterr().out


def test_an_id_names_the_same_test_from_any_directory(tree, monkeypatch, capsys):
    assert main(run_args("--id", "b/z_fail.test::z_fail", "--no-binary-check")) == 1
    monkeypatch.chdir(tree / "test" / "a")
    assert main(run_args("--id", "b/z_fail.test::z_fail", "--no-binary-check")) == 1
    assert capsys.readouterr().out.count("FAILED   b/z_fail.test::z_fail") == 2


def test_lldb_starts_the_debugger_on_the_one_selected_test(tree, monkeypatch, capsys):
    record = tree / "debugger.json"
    monkeypatch.setenv("FAKE_DEBUGGER_RECORD", str(record))
    monkeypatch.setenv("DUCKTEST_DEBUGGER", f"{sys.executable} {DEBUGGER} --")
    assert main(run_args("--lldb", "--id", "a/x.test::x", "--no-binary-check")) == 0
    argv = json.loads(record.read_text())["argv"]
    assert argv[:4] == ["--", FAKE, "--test-dir", str(tree)]
    assert "batch kept: " in capsys.readouterr().out


def test_lldb_on_more_than_one_test_is_a_usage_error_that_lists_them(tree, capsys):
    assert main(run_args("--lldb", "test", "--no-binary-check")) == 2
    err = capsys.readouterr().err
    assert "exactly one test of a .test file" in err and "a/x.test::x" in err and "b/z_fail.test::z_fail" in err


def test_an_excluded_file_that_the_binary_knows_is_not_a_stale_build(tree, monkeypatch, capsys):
    known = tree / "known.txt"
    known.write_text("test/a/x.test\ntest/a/y.test_slow\ntest/b/z_fail.test\ntest/generated/g.test\n")
    monkeypatch.setenv("FAKE_UNITTEST_LIST", str(known))
    assert main(run_args("test/a")) == 0

    known.write_text("test/a/x.test\ntest/generated/g.test\ntest/gone.test\n")
    assert main(run_args("test/a")) == 2
    err = capsys.readouterr().err
    assert "test/gone.test" in err and "test/b/z_fail.test" in err and "generated" not in err
