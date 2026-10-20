"""The example ladder, run the way its README says: `scan --env`, `select --explain` and `run` in
each rung, against the goldens in ducktest_goldens/ladder/. Re-make them with
`UPDATE_GOLDENS=1 pytest tests/test_ladder.py`.

`run` goes through the fake binaries always, and through a built duckdb and test/unittest when
$DUCKDB_SHELL names one; both must print the same golden. The golden of a run ends with the batches
of its plan, the key digests numbered in the order they appear, so a reader sees which tests share
one identity. Each rung runs on a copy, so nothing is written into examples/.
"""

import os
import re
import shutil
import sys
import tempfile

import pytest
from ducktest_real_build import missing
import yaml

from ducktest.cli import main
from ducktest.resources.credential import sdk_prefixes

HERE = os.path.dirname(os.path.abspath(__file__))
LADDER = os.path.join(os.path.dirname(HERE), "examples", "ladder")
GOLDENS = os.path.join(HERE, "ducktest_goldens", "ladder")
FAKES = os.path.join(HERE, "ducktest_fakes")
RUNGS = sorted(d for d in os.listdir(LADDER) if os.path.isdir(os.path.join(LADDER, d)))
# A rung whose README selects by path gives the same arguments to `select` and `run`.
ARGS = {"00_existing": ["test/sql/math", "--suite", "default"]}
NEEDS = {
    "04_service": ("httpfs",),
    "03_credentials": ("httpfs", "aws"),
    "05_s3": ("httpfs", "aws"),
    "06_azurite": ("azure",),
    "07_storage_matrix": ("httpfs", "aws"),
}
EXIT_CODES = {"04_service": 1, "05_s3": 1, "06_azurite": 1, "07_storage_matrix": 1}  # a blocked test fails the run
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


def results_in_id_order(text):
    """The `[i/N]` result lines sorted by test id and numbered again: a batch is reported by the
    main thread once it sees it ended, a Python body by its worker, so even one worker does not fix
    the order of the two."""
    lines, out, block = text.split("\n"), [], []
    for line in [*lines, None]:
        if line is not None and re.match(r"^ *\[\d+/\d+\] ", line):
            block.append(line)
            continue
        if block:
            rest = sorted((line.split("] ", 1)[1] for line in block), key=lambda r: r.split()[1])
            width = len(f"[{len(rest)}/{len(rest)}]")  # the report pads the counter to the width of the last
            out += [f"{f'[{n}/{len(rest)}]':>{width}} {r}" for n, r in enumerate(rest, 1)]
            block = []
        if line is not None:
            out.append(line)
    return "\n".join(out)


def normalized(text, tmp_path):
    text = results_in_id_order(text).replace(str(tmp_path), "<tmp>")
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
        lines.append(f"  {batch['name']}  {batch['runner']:<8}  {key}  {', '.join(batch['files'])}")
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
    for name in [n for n in os.environ if n.startswith(sdk_prefixes())]:  # the goldens list what the shell's env holds
        monkeypatch.delenv(name)
    # Nothing answers there, so the servers of rungs 05 to 07 are BLOCKED the same way with or without docker.
    monkeypatch.setenv("DUCKTEST_EXISTING_SERVICE_LOCAL_S3", "http://127.0.0.1:9")
    monkeypatch.setenv("DUCKTEST_EXISTING_SERVICE_LOCAL_AZ", "http://127.0.0.1:9")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def copy(rung):
        where = tmp_path / "repo" / rung
        shutil.copytree(
            os.path.join(LADDER, rung), where, ignore=shutil.ignore_patterns("__pycache__", "ducktest_temp")
        )
        if rung == "00_existing":
            (where / ".git").mkdir()  # its top is a git repository's, where {DATA_DIR} is found
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
    assert main(["select", "--explain", *ARGS.get(rung, [])]) == 0
    golden(f"{rung}.select-explain.txt", normalized(capsys.readouterr().out, tmp_path))


@pytest.mark.parametrize("binaries", ["fake", "real"])
@pytest.mark.parametrize("rung", RUNGS)
def test_run(rung, binaries, rung_copy, tmp_path, capsys):
    if BINARIES[binaries] is None:
        pytest.skip("no built duckdb and test/unittest (set $DUCKDB_SHELL)")
    shell, unittest = BINARIES[binaries]
    if binaries == "real" and (lacking := missing(shell, *NEEDS.get(rung, ()))):
        pytest.skip(f"the build at {shell} has no {', '.join(lacking)}")
    rung_copy(rung)
    runs = tmp_path / "runs"
    args = ["--duckdb-bin", shell, "--unittest-bin", unittest, "--temp-dir-root", str(runs)]
    # --keep leaves every file of the run for the search below.
    code = main(["run", *args, "--workers", "1", "--keep", *ARGS.get(rung, [])])
    out = capsys.readouterr().out
    assert code == EXIT_CODES.get(rung, 0), out
    (run_dir,) = runs.iterdir()
    golden(f"{rung}.run.txt", normalized(out, tmp_path) + plan_batches(run_dir))
    assert holding(tmp_path, FAKE_KEY.values()) == []


def test_the_run_s_debug_log_never_holds_the_fake_secret(rung_copy, tmp_path, capsys):
    """DEBUG diagnostics see every subprocess command line and template resolution; none of that may
    put a real value where a secret's name says one belongs (rung 03's credential, LADDER_FAKE_*)."""
    rung_copy("03_credentials")
    runs = tmp_path / "runs"
    shell, unittest = BINARIES["fake"]
    args = ["--duckdb-bin", shell, "--unittest-bin", unittest, "--temp-dir-root", str(runs)]
    assert main(["run", *args, "-vv", "--keep"]) == 0, capsys.readouterr().out
    (run_dir,) = runs.iterdir()
    log = run_dir / "ducktest.log"
    assert "DEBUG" in log.read_text()
    assert holding(tmp_path, FAKE_KEY.values()) == []


@pytest.mark.parametrize("binaries", ["fake", "real"])
def test_a_green_run_leaves_nothing_under_its_storages(binaries, rung_copy, tmp_path, capsys):
    if BINARIES[binaries] is None:
        pytest.skip("no built duckdb and test/unittest (set $DUCKDB_SHELL)")
    shell, unittest = BINARIES[binaries]
    rung_copy("02_rules")
    runs = tmp_path / "runs"
    args = ["--duckdb-bin", shell, "--unittest-bin", unittest, "--temp-dir-root", str(runs)]
    assert main(["run", *args]) == 0, capsys.readouterr().out
    (run_dir,) = runs.iterdir()
    assert [p for p in (tmp_path / "tmp").rglob("*") if p.is_file() or p.name == "temp"] == []
    assert list((run_dir / "tests").iterdir()) == []
