"""ducktest5's command line, on a small tree, with the fake unittest binary."""

import json
import os
import re
import shutil
import sys
import textwrap

import pytest

from ducktest5.binary import UsageError, find_duckdb
from ducktest5.cli import main
from ducktest5.sqllogic import find_unittest

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
                process_env={"AWS_PROFILE": "ducktest", "AWS_ACCESS_KEY_ID": None},
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
          process env: AWS_PROFILE=ducktest  <- AXES storage=s3
          process env unset: AWS_ACCESS_KEY_ID  <- AXES storage=s3
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


# -----------------------------------------------------------------------------
# The default storage
#


def data_dir_line(out):
    (line,) = {line.strip() for line in out.splitlines() if "root: data_dir=" in line}
    return line


@pytest.mark.parametrize("git", ["dir", "file"])  # a worktree's .git is a file
def test_the_default_data_dir_is_data_at_the_top_of_the_repository_from_any_directory_in_it(
    tree, monkeypatch, capsys, git
):
    (tree / ".git").mkdir() if git == "dir" else (tree / ".git").write_text("gitdir: elsewhere\n")
    assert main(["scan", "--env"]) == 0
    from_root = data_dir_line(capsys.readouterr().out)
    monkeypatch.chdir(tree / "test" / "a")
    assert main(["scan", "--env", "x.test"]) == 0
    assert data_dir_line(capsys.readouterr().out) == from_root == f"root: data_dir={tree}/data  <- default storage"


def test_outside_a_repository_the_default_data_dir_is_data_under_the_project_root(tree, capsys):
    assert main(["scan", "--env"]) == 0
    assert data_dir_line(capsys.readouterr().out) == f"root: data_dir={tree}/test/data  <- default storage"


def test_the_command_line_moves_the_default_storage_for_the_run(tree, capsys):
    assert main(["scan", "--env", "test/a/x.test", "--data-dir", "fixtures", "--temp-dir-root", "scratch"]) == 0
    out = capsys.readouterr().out
    assert f"root: temp_dir_root={tree}/scratch/<run_token>  <- default storage" in out
    assert f"root: data_dir={tree}/fixtures  <- default storage" in out
    assert main([*run_args("test/a/x.test", "--keep", "--no-binary-check"), "--data-dir", "fixtures"]) == 0
    (run_dir,) = (tree / "runs").iterdir()
    (config,) = run_dir.glob("batches/*/config.json")
    given = json.loads(config.read_text())
    assert (given["data_dir"], given["temp_dir_root"]) == (f"{tree}/fixtures", str(run_dir))


STORAGE_CONF = """
    from ducktest5 import default
    from ducktest5.resources import StorageDef
    STORAGES = [StorageDef("scratch", "/srv/scratch"{data})]
    RULES = [default(bind={{"storage": "scratch"}})]
"""


def test_a_storage_bound_in_the_conf_wins_over_the_default_and_the_command_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "ducktest_conf.py").write_text(textwrap.dedent(STORAGE_CONF.format(data=', data="/srv/data"')))
    (tmp_path / "x.test").write_text("")
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--env", "--data-dir", "fixtures", "--temp-dir-root", "scratch"]) == 0
    out = capsys.readouterr().out
    assert "root: temp_dir_root=/srv/scratch/temp/<run_token>  <- storage scratch" in out
    assert data_dir_line(out) == "root: data_dir=/srv/data  <- storage scratch"
    assert "default storage" not in out


def test_a_bound_storage_that_declares_no_data_leaves_the_data_dir_to_the_default(tmp_path, monkeypatch, capsys):
    (tmp_path / "ducktest_conf.py").write_text(textwrap.dedent(STORAGE_CONF.format(data="")))
    (tmp_path / "x.test").write_text("")
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--env"]) == 0
    out = capsys.readouterr().out
    assert "root: temp_dir_root=/srv/scratch/temp/<run_token>  <- storage scratch" in out
    assert data_dir_line(out) == f"root: data_dir={tmp_path}/data  <- default storage"


# -----------------------------------------------------------------------------
# Where the build is found
#


def build_in(where, build="debug"):
    for name in ("duckdb", "test/unittest"):
        path = where / "build" / build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FAKE, path)
    return where / "build" / build


def test_a_build_is_looked_for_at_the_top_of_the_repository_then_in_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("BUILD_DIR", raising=False)
    repo, cwd = tmp_path / "repo", tmp_path / "repo" / "test" / "sql"
    cwd.mkdir(parents=True)
    places = (str(repo), str(cwd))
    with pytest.raises(UsageError, match=re.escape(f"{repo}/build/debug/duckdb or {cwd}/build/debug/duckdb")):
        find_duckdb(places=places)
    in_cwd = build_in(cwd)
    assert find_unittest(places=places) == str(in_cwd / "test" / "unittest")
    in_repo = build_in(repo)
    assert find_duckdb(places=places) == str(in_repo / "duckdb")
    assert find_unittest(places=places) == str(in_repo / "test" / "unittest")
    monkeypatch.setenv("BUILD_DIR", str(in_cwd))
    assert find_duckdb(places=places) == str(in_cwd / "duckdb")
    assert find_duckdb(explicit=FAKE, places=places) == FAKE


def test_run_finds_the_build_of_the_repository_from_a_directory_below_it(tree, monkeypatch, capsys):
    monkeypatch.delenv("BUILD_DIR", raising=False)
    (tree / ".git").mkdir()
    build_in(tree)
    monkeypatch.chdir(tree / "test" / "a")
    assert main(["run", "x.test", "--no-binary-check", "--temp-dir-root", str(tree / "runs"), "--format", "json"]) == 0
    assert f"{tree}/build/debug/test/unittest" in capsys.readouterr().out


# -----------------------------------------------------------------------------
# Diagnostic logging: what the tool did, apart from the report (`--format`, `--level`)
#


def test_by_default_stderr_is_quiet(tree, capsys):
    assert main(run_args("test/a/x.test", "--no-binary-check")) == 0
    assert capsys.readouterr().err == ""


def test_v_puts_info_on_stderr_but_not_debug(tree, capsys):
    assert main(run_args("test/a/x.test", "--no-binary-check", "-v")) == 0
    err = capsys.readouterr().err
    assert "INFO" in err and "run start:" in err
    assert "DEBUG" not in err


def test_vv_puts_debug_on_stderr_too(tree, capsys):
    assert main(run_args("test/a/x.test", "--no-binary-check", "-vv")) == 0
    err = capsys.readouterr().err
    assert "DEBUG" in err


def test_vv_does_not_change_a_terse_report_on_stdout(tree, capsys):
    def once(*more):
        assert main(run_args("test/a/x.test", "--no-binary-check", "--level", "terse", *more)) == 0
        out = capsys.readouterr().out
        (run_dir,) = (tree / "runs").iterdir()
        shutil.rmtree(run_dir)
        out = out.replace(str(run_dir), "<run-dir>")
        return re.sub(r"\d+(\.\d+)?s;", "<s>;", out)

    assert once() == once("-vv")


def test_the_run_dir_always_holds_a_debug_log(tree, capsys):
    assert main(run_args("test/a/x.test", "--no-binary-check")) == 0  # no -v at all
    (run_dir,) = (tree / "runs").iterdir()
    log = (run_dir / "ducktest.log").read_text()
    assert "DEBUG" in log
    assert "INFO" in log and "run start:" in log
