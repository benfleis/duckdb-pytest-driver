"""`ducktest init`: write a repo's test environment, `pyproject.toml`, and lock it.

The file pins the runner by git URL and rev (or a local checkout, editable) and lists what the tests
import. A file that is already there is never overwritten without `--write`: init prints the diff
from it to what it would write, and the user decides. It also makes sure `.gitignore` ignores the
runner's default temp root.
"""

import difflib
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution

from .binary import UsageError
from .declare import CONF_FILE
from .resources.storage import DEFAULT_TEMP_ROOT

DIST = "duckdb-ducktest"
RUNNER_GIT = "https://github.com/benfleis/duckdb-pytest-driver.git"
PYTHON = ">=3.12"

STARTER_CONF = '''\
# What the tests under this directory need: engines, services, storages, credentials, rules.
# Every field is optional, and a tree of plain .test files needs no file at all.
# For the fields, see the ducktest docs: design.md, "Options" and "The conf file"; examples/ladder/ shows each idea.

# from ducktest import DuckDBEngineDef, Project, default, match
#
# project = Project(
#     engines=[DuckDBEngineDef("duck", requires=["parquet"])],
#     rules=[
#         default(engines=["duck"]),
#         match("slow/**", tags=["opt-in"]),
#     ],
# )
'''


def add_parser(sub):
    p = sub.add_parser("init", help="write the repo's test environment (pyproject.toml) and lock it")
    p.add_argument(
        "--dir", metavar="DIR", help="the repo to write into (default: the top of the git repository you are in)"
    )
    p.add_argument("--name", help="the project name (default: <name of the repo>-tests)")
    p.add_argument(
        "--runner",
        metavar="SPEC",
        help="where the runner comes from: git+URL@REV, or path:DIR for a local checkout, editable "
        "(default: where this ducktest was installed from)",
    )
    p.add_argument("--extra", action="append", default=[], metavar="NAME", help=f"an extra of {DIST}; repeats")
    p.add_argument(
        "--dep", action="append", default=[], metavar="REQ", help="a package the tests import, e.g. pyspark==4.2.0"
    )
    p.add_argument(
        "--conf",
        nargs="?",
        const="test",
        metavar="DIR",
        help=f"also write a starter {CONF_FILE} in DIR, relative to the repo (default: test)",
    )
    p.add_argument("--write", action="store_true", help="overwrite a file that is there and differs")
    p.add_argument("--no-lock", action="store_true", help="do not run `uv lock` after")
    return p


def run(args):
    repo = _repo(args.dir)
    _ensure_gitignore(repo)
    source = _runner_source(args.runner)
    name = args.name or f"{_repo_name(repo)}-tests"
    rc = 0
    pyproject = os.path.join(repo, "pyproject.toml")
    placed = _place(pyproject, render(name, source, args.extra, args.dep), args.write)
    if placed == "differs":
        rc = 1
    if args.conf is not None:
        conf_dir = os.path.join(repo, args.conf)
        os.makedirs(conf_dir, exist_ok=True)
        if _place(os.path.join(conf_dir, CONF_FILE), STARTER_CONF, args.write) == "differs":
            rc = 1
    if rc:
        print(
            "ducktest: init did not overwrite the files above; merge the diff, or run again with --write",
            file=sys.stderr,
        )
        return rc
    if args.no_lock:
        return 0
    if not shutil.which("uv"):
        print(f"uv is not on PATH: run `uv lock` in {repo}, and check uv.lock in")
        return 0
    sys.stdout.flush()
    return subprocess.call(["uv", "lock"], cwd=repo)


def render(name, source, extras=(), deps=()):
    """The pyproject.toml of a repo's test environment."""
    runner = DIST + (f"[{','.join(extras)}]" if extras else "")
    lines = "".join(f'  "{d}",\n' for d in [runner, *deps])
    return f"""\
# The environment of this repo's tests, written by `ducktest init`; yours to edit.
#   uv sync && .venv/bin/ducktest run
[project]
name = "{name}"
version = "0"
requires-python = "{PYTHON}"
dependencies = [
{lines}]

[tool.uv]
package = false

[tool.uv.sources]
{DIST} = {source}

# Helper directories, relative to this file, on sys.path before any ducktest_conf.py runs.
[tool.ducktest]
pythonpath = []
"""


def parse_runner(spec):
    """`git+URL@REV` or `path:DIR` as a [tool.uv.sources] value."""
    if spec.startswith("path:"):
        return f'{{ path = "{os.path.abspath(spec[len("path:") :])}", editable = true }}'
    if spec.startswith("git+"):
        url = spec[len("git+") :]
        # A rev is the part after an `@` in the URL's path; an `@` before it is a user (git@host).
        path_at = url.find("/", url.find("://") + 3) if "://" in url else -1
        at = url.rfind("@")
        if path_at >= 0 and at > path_at:
            return f'{{ git = "{url[:at]}", rev = "{url[at + 1 :]}" }}'
        raise UsageError(f"--runner {spec}: pin a rev, git+URL@REV")
    raise UsageError(f"--runner {spec}: give git+URL@REV or path:DIR")


def _runner_source(spec):
    if spec:
        return parse_runner(spec)
    try:
        direct = json.loads(distribution(DIST).read_text("direct_url.json") or "null")
    except PackageNotFoundError:
        direct = None
    if direct and "dir_info" in direct and direct["url"].startswith("file://"):
        return parse_runner("path:" + direct["url"][len("file://") :])
    if direct and direct.get("vcs_info", {}).get("vcs") == "git":
        return f'{{ git = "{direct["url"]}", rev = "{direct["vcs_info"]["commit_id"]}" }}'
    try:
        version = distribution(DIST).version
    except PackageNotFoundError:
        raise UsageError(f"cannot tell where {DIST} came from; give --runner git+URL@REV") from None
    return f'{{ git = "{RUNNER_GIT}", tag = "v{version}" }}'


def _ensure_gitignore(repo):
    """Make sure `.gitignore` ignores the runner's default temp root. Additive, so unlike
    `pyproject.toml` it needs no diff/--write gate."""
    path = os.path.join(repo, ".gitignore")
    entry = f"{DEFAULT_TEMP_ROOT}/"
    ignored = {DEFAULT_TEMP_ROOT, entry, f"/{DEFAULT_TEMP_ROOT}", f"/{entry}"}
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(entry + "\n")
        print(f"{path}: written, ignoring {entry}")
        return
    with open(path) as f:
        have = f.read()
    if any(line.strip() in ignored for line in have.splitlines()):
        print(f"{path}: already ignores {DEFAULT_TEMP_ROOT}")
        return
    with open(path, "a") as f:
        if have and not have.endswith("\n"):
            f.write("\n")
        f.write(entry + "\n")
    print(f"{path}: appended {entry}")


def _repo(given):
    if given:
        if not os.path.isdir(given):
            raise UsageError(f"--dir {given}: not a directory")
        return os.path.abspath(given)
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if out.returncode:
        raise UsageError("not in a git repository; give the repo with --dir")
    return out.stdout.strip()


def _repo_name(repo):
    """The name of the origin remote (a worktree's directory is often named for its branch), else of
    the directory."""
    out = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True)
    url = out.stdout.strip().rstrip("/") if out.returncode == 0 else ""
    name = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if url else os.path.basename(repo)
    return name.removesuffix(".git")


def _place(path, want, write):
    """'wrote', 'same', or 'differs' (the diff printed, nothing written)."""
    if os.path.exists(path):
        with open(path) as f:
            have = f.read()
        if have == want:
            print(f"{path}: up to date")
            return "same"
        if not write:
            print(f"{path}: there and different; what init would change:", file=sys.stderr, flush=True)
            name = os.path.basename(path)
            sys.stdout.writelines(
                difflib.unified_diff(
                    have.splitlines(True),
                    want.splitlines(True),
                    fromfile=f"{name} (on disk)",
                    tofile=f"{name} (ducktest init)",
                )
            )
            sys.stdout.flush()
            return "differs"
    with open(path, "w") as f:
        f.write(want)
    print(f"{path}: written")
    return "wrote"
