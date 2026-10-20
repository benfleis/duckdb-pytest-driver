"""`ducktest init`, in throwaway git repos; `uv lock` is a fake on PATH, or left out."""

import os
import stat
import subprocess
import sys

import pytest

from ducktest.cli import main
from ducktest.init import STARTER_CONF, render
from ducktest.resources.storage import DEFAULT_TEMP_ROOT
from ducktest.scan import scan

LOCAL = "path:/src/runner"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = tmp_path / "checkout"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "git@github.com:duckdb/duckdb-delta.git"], check=True
    )
    monkeypatch.chdir(path)
    return path


def test_writes_the_test_environment(repo, capsys):
    rc = main(["init", "--runner", LOCAL, "--extra", "s3", "--dep", "pyspark==4.2.0", "--no-lock"])
    assert rc == 0
    assert (repo / "pyproject.toml").read_text() == (
        "# The environment of this repo's tests, written by `ducktest init`; yours to edit.\n"
        "#   uv sync && .venv/bin/ducktest run\n"
        "[project]\n"
        'name = "duckdb-delta-tests"\n'
        'version = "0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = [\n"
        '  "duckdb-ducktest[s3]",\n'
        '  "pyspark==4.2.0",\n'
        "]\n"
        "\n"
        "[tool.uv]\n"
        "package = false\n"
        "\n"
        "[tool.uv.sources]\n"
        'duckdb-ducktest = { path = "/src/runner", editable = true }\n'
        "\n"
        "# Helper directories, relative to this file, on sys.path before any ducktest_conf.py runs.\n"
        "[tool.ducktest]\n"
        "pythonpath = []\n"
    )
    assert not (repo / "test").exists()


def test_python_version_is_pinned_when_missing(repo, capsys):
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 0
    assert (repo / ".python-version").read_text() == "3.12\n"
    assert "pinning Python 3.12" in capsys.readouterr().out


def test_an_existing_python_version_is_left_alone(repo, capsys):
    (repo / ".python-version").write_text("3.13\n")
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 0
    assert (repo / ".python-version").read_text() == "3.13\n"
    assert "already pins 3.13" in capsys.readouterr().out


def test_gitignore_is_created_when_missing(repo, capsys):
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 0
    assert (repo / ".gitignore").read_text() == f"{DEFAULT_TEMP_ROOT}/\n"
    assert f"written, ignoring {DEFAULT_TEMP_ROOT}/" in capsys.readouterr().out


@pytest.mark.parametrize(
    "line", [f"{DEFAULT_TEMP_ROOT}/", DEFAULT_TEMP_ROOT, f"/{DEFAULT_TEMP_ROOT}/", f"/{DEFAULT_TEMP_ROOT}"]
)
def test_gitignore_already_ignoring_it_is_left_alone(repo, capsys, line):
    before = f"*.pyc\n{line}\nbuild/\n"
    (repo / ".gitignore").write_text(before)
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 0
    assert (repo / ".gitignore").read_text() == before
    assert f"already ignores {DEFAULT_TEMP_ROOT}" in capsys.readouterr().out


@pytest.mark.parametrize("before", ["*.pyc\n", "*.pyc"], ids=["trailing-newline", "no-trailing-newline"])
def test_gitignore_gets_the_line_appended(repo, capsys, before):
    (repo / ".gitignore").write_text(before)
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 0
    assert (repo / ".gitignore").read_text() == f"*.pyc\n{DEFAULT_TEMP_ROOT}/\n"
    assert f"appended {DEFAULT_TEMP_ROOT}/" in capsys.readouterr().out


@pytest.mark.parametrize(
    "spec, source",
    [
        ("git+https://github.com/o/r.git@dev/v0.5", '{ git = "https://github.com/o/r.git", rev = "dev/v0.5" }'),
        ("git+ssh://git@github.com/o/r.git@8305ad9", '{ git = "ssh://git@github.com/o/r.git", rev = "8305ad9" }'),
    ],
)
def test_runner_by_git_url_and_rev(repo, spec, source):
    assert main(["init", "--runner", spec, "--no-lock"]) == 0
    assert f"duckdb-ducktest = {source}\n" in (repo / "pyproject.toml").read_text()


@pytest.mark.parametrize("spec", ["git+ssh://git@github.com/o/r.git", "https://github.com/o/r.git"])
def test_runner_without_a_rev_or_a_kind_is_a_usage_error(repo, spec, capsys):
    assert main(["init", "--runner", spec, "--no-lock"]) == 2
    assert "--runner" in capsys.readouterr().err
    assert not (repo / "pyproject.toml").exists()


def test_default_runner_is_where_this_one_came_from(repo):
    assert main(["init", "--no-lock"]) == 0
    assert "\nduckdb-ducktest = { " in (repo / "pyproject.toml").read_text()


def test_a_different_file_is_diffed_not_overwritten(repo, capsys):
    mine = render("duckdb-delta-tests", '{ path = "/src/runner", editable = true }').replace(
        "dependencies = [\n", 'dependencies = [\n  "boto3",\n'
    )
    (repo / "pyproject.toml").write_text(mine)
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 1
    out, err = capsys.readouterr()
    assert "--- pyproject.toml (on disk)\n+++ pyproject.toml (ducktest init)\n" in out
    assert '\n-  "boto3",\n' in out
    assert "--write" in err
    assert (repo / "pyproject.toml").read_text() == mine

    assert main(["init", "--runner", LOCAL, "--no-lock", "--write"]) == 0
    assert '"boto3"' not in (repo / "pyproject.toml").read_text()
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_starter_conf_only_when_asked(repo):
    assert main(["init", "--runner", LOCAL, "--no-lock", "--conf"]) == 0
    assert (repo / "test" / "ducktest_conf.py").read_text() == STARTER_CONF
    (repo / "test" / "a.test").write_text("")
    tests, _ = scan(str(repo / "test"), working_dir=str(repo))
    assert [t.name for t in tests] == ["a.test::a"]

    assert main(["init", "--runner", LOCAL, "--no-lock", "--conf", "sql"]) == 0
    assert (repo / "sql" / "ducktest_conf.py").exists()


def test_name_without_a_remote_is_the_directory(tmp_path, monkeypatch):
    (tmp_path / "myext").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--dir", "myext", "--runner", LOCAL, "--no-lock"]) == 0
    assert 'name = "myext-tests"' in (tmp_path / "myext" / "pyproject.toml").read_text()


def test_outside_a_git_repository_needs_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert main(["init", "--runner", LOCAL, "--no-lock"]) == 2
    assert "--dir" in capsys.readouterr().err


def test_locks_with_uv(repo, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport os, sys\nopen({str(tmp_path / 'called')!r}, 'w').write(os.getcwd() + ' ' + ' '.join(sys.argv[1:]))\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert main(["init", "--runner", LOCAL]) == 0
    assert (tmp_path / "called").read_text() == f"{repo} lock"


def test_no_uv_says_how(repo, monkeypatch, capsys):
    monkeypatch.setattr("ducktest.init.shutil.which", lambda name: None)
    assert main(["init", "--runner", LOCAL]) == 0
    assert "run `uv lock`" in capsys.readouterr().out
