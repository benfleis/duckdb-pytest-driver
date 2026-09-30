"""The example ladder, run the way its README says: `scan --env`, `select --explain` and `run` in
each rung, against the goldens in ducktest5_goldens/ladder/. Re-make them with
`UPDATE_GOLDENS=1 pytest tests/test_ducktest5_ladder.py`.

`run` goes through the fake binaries always, and through a built duckdb and test/unittest when
$DUCKDB_SHELL names one; both must print the same golden. The golden of a run ends with the batches
of its plan, the key digests numbered in the order they appear, so a reader sees which tests share
one Session identity. Each rung runs on a copy, so nothing is written into examples/.
"""

import os
import re
import shutil
import sys
import tempfile

import pytest
import yaml

from ducktest5.cli import main

HERE = os.path.dirname(os.path.abspath(__file__))
LADDER = os.path.join(os.path.dirname(HERE), "examples", "ladder")
GOLDENS = os.path.join(HERE, "ducktest5_goldens", "ladder")
FAKES = os.path.join(HERE, "ducktest5_fakes")
RUNGS = sorted(d for d in os.listdir(LADDER) if os.path.isdir(os.path.join(LADDER, d)))
EXIT_CODES = {"04_service": 1}  # its blocked test fails the run
FAKE_KEY = {"LADDER_FAKE_KEY_ID": "ladder-key-id-7f3a", "LADDER_FAKE_SECRET": "ladder-secret-9c1e"}


def _real_binaries():
    shell = os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")
    unittest = os.path.join(os.path.dirname(shell or ""), "test", "unittest")
    return (shell, unittest) if shell and os.path.isfile(unittest) else None


BINARIES = {
    "fake": (os.path.join(FAKES, "fake_shell.py"), os.path.join(FAKES, "fake_unittest.py")),
    "real": _real_binaries(),
}


def golden(name, produced):
    path = os.path.join(GOLDENS, name)
    if os.environ.get("UPDATE_GOLDENS"):
        os.makedirs(GOLDENS, exist_ok=True)
        with open(path, "w") as f:
            f.write(produced)
    with open(path) as f:
        assert produced == f.read(), f"{name} differs; UPDATE_GOLDENS=1 to re-make it"


def normalized(text, tmp_path):
    text = text.replace(str(tmp_path), "<tmp>")
    text = re.sub(r"<tmp>/runs/[^/\s]+", "<run>", text)
    text = re.sub(r"\(\d+(?:\.\d+)?s(?:: [^)]*)?\)", "(<s>)", text)
    text = re.sub(r" in \d+(?:\.\d+)?s$", " in <s>", text, flags=re.M)
    return re.sub(r"\nslowest:\n(?:.+\n)+", "", text)


def plan_batches(run_dir):
    with open(os.path.join(run_dir, "02-plan.yaml")) as f:
        plan = yaml.safe_load(f)
    keys, lines = {}, ["", "02-plan.yaml batches:"]
    for batch in plan["batches"]:
        key = keys.setdefault(batch["key"]["digest"], f"key{len(keys) + 1}")
        lines.append(f"  {batch['id']}  {batch['runner']:<8}  {key}  {', '.join(batch['files'])}")
    return "\n".join(lines) + "\n"


def holding(root, values):
    """The files under `root` that hold any of `values`."""
    found = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                content = f.read()
            if any(v.encode() in content for v in values):
                found.append(path)
    return found


@pytest.fixture
def rung_copy(tmp_path, monkeypatch):
    """Works in a copy of a rung, with the fake key set and the system temp directory in tmp_path."""
    for name, value in FAKE_KEY.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def copy(rung):
        where = tmp_path / "repo" / rung
        shutil.copytree(
            os.path.join(LADDER, rung), where, ignore=shutil.ignore_patterns("__pycache__", "duckdb_unittest_tempdir")
        )
        monkeypatch.chdir(where)

    return copy


@pytest.mark.parametrize("rung", RUNGS)
def test_scan_env(rung, rung_copy, tmp_path, capsys):
    rung_copy(rung)
    assert main(["scan", "--env"]) == 0
    golden(f"{rung}.scan-env.txt", normalized(capsys.readouterr().out, tmp_path))


@pytest.mark.parametrize("rung", RUNGS)
def test_select_explain(rung, rung_copy, tmp_path, capsys):
    rung_copy(rung)
    assert main(["select", "--explain"]) == 0
    golden(f"{rung}.select-explain.txt", normalized(capsys.readouterr().out, tmp_path))


@pytest.mark.parametrize("binaries", ["fake", "real"])
@pytest.mark.parametrize("rung", RUNGS)
def test_run(rung, binaries, rung_copy, tmp_path, capsys):
    if BINARIES[binaries] is None:
        pytest.skip("no built duckdb and test/unittest (set $DUCKDB_SHELL)")
    shell, unittest = BINARIES[binaries]
    rung_copy(rung)
    runs = tmp_path / "runs"
    args = ["--duckdb-bin", shell, "--unittest-bin", unittest, "--no-binary-check", "--temp-dir-root", str(runs)]
    # One worker runs the plan in order; --keep leaves every file of the run for the search below.
    code = main(["run", *args, "--workers", "1", "--keep"])
    out = capsys.readouterr().out
    assert code == EXIT_CODES.get(rung, 0), out
    (run_dir,) = runs.iterdir()
    golden(f"{rung}.run.txt", normalized(out, tmp_path) + plan_batches(run_dir))
    assert holding(tmp_path, FAKE_KEY.values()) == []
