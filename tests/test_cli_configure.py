"""`ducktest configure` writes two files with DIFFERENT ownership semantics:

- pytest.ini is OWNED — kept byte-exact; a hand-edited copy stops configure with a diff (rc=1), never
  clobbered.
- pyproject.toml is SCAFFOLDED — written once when absent, then left alone forever (it's the test venv,
  which you fill with real deps), so an existing one is never a failure and never overwritten.
"""

import types

from ducktest.cli import _PYPROJECT, _PYTEST_INI, _configure


def _run(tmp_path):
    return _configure(types.SimpleNamespace(dir=str(tmp_path)))


def test_writes_both_files_on_a_fresh_repo(tmp_path):
    rc = _run(tmp_path)
    assert rc == 0
    assert (tmp_path / "pytest.ini").read_text() == _PYTEST_INI
    assert (tmp_path / "pyproject.toml").read_text() == _PYPROJECT
    # the scaffold must actually be usable as the "one venv" anchor
    body = (tmp_path / "pyproject.toml").read_text()
    assert "[dependency-groups]" in body
    assert "package = false" in body  # extension, not an installable package


def test_existing_pyproject_is_left_untouched(tmp_path):
    # A real repo's pyproject carries its own deps — configure must not clobber or fail on it.
    mine = '[project]\nname = "my-ext-tests"\n\n[dependency-groups]\ndev = ["pyspark==4.0.1"]\n'
    (tmp_path / "pyproject.toml").write_text(mine)
    rc = _run(tmp_path)
    assert rc == 0  # not a failure
    assert (tmp_path / "pyproject.toml").read_text() == mine  # verbatim, my deps safe
    assert (tmp_path / "pytest.ini").exists()  # the owned file still gets written alongside


def test_idempotent_second_run(tmp_path):
    assert _run(tmp_path) == 0
    # second run: pytest.ini already matches, pyproject already exists → both no-ops, still rc=0
    assert _run(tmp_path) == 0
    assert (tmp_path / "pyproject.toml").read_text() == _PYPROJECT


def test_hand_edited_pytest_ini_is_not_clobbered(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q\n")  # diverges from the template
    rc = _run(tmp_path)
    assert rc == 1  # owned file diverged → configure fails loud rather than overwriting
    assert (tmp_path / "pytest.ini").read_text() == "[pytest]\naddopts = -q\n"  # untouched
    # scaffold still lands on the same run (independent of the owned-file failure)
    assert (tmp_path / "pyproject.toml").read_text() == _PYPROJECT
