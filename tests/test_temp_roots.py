"""Self-tests for the TEMP/DATA storage-input origination + passthrough (SPEC §11.3).

FOLLOWING the in-main temp-dir model, the driver originates ONLY ``$BASE`` + ``$RUN_ID`` (the run
mnemonic) plus an optional read-only DATA dir, and passes ``--temp-dir-base`` / ``--run-id``
(+ ``--temp-dir-destroy``, + ``--data-dir`` when set) to EVERY unittest invocation. The BINARY composes
``TEMP_DIR=$BASE/$RUN_ID``, derives ``LOCAL_*``, and owns local create/reap. These exercise the pure
origination (`_temp_roots`) offline and prove the corrected flags reach the subprocess — and that the
OLD wrong passthrough (``--temp-dir`` exact + a ``TEMP_DIR`` env var) does NOT — against a stub binary
that echoes its argv/env.
"""

import stat
import tempfile
import textwrap

import pytest

from ducktest.plugin import _is_remote_root, _temp_roots
from ducktest.sqllogic import _invoke

RUN_ID = "2026-07-18T00-00-00Z--brave-fox-42"

# The four env vars the OLD (wrong) driver set; here we assert the driver sets NONE of them (the binary
# composes/derives + overwrites them). The stub echoes each so we can prove it stays unset.
_ROOT_VARS = ("TEMP_DIR", "LOCAL_TEMP_DIR", "DATA_DIR", "LOCAL_DATA_DIR")


class _Cfg:
    """Minimal stand-in for a pytest Config: a fixed run-id + the options `_temp_roots` reads."""

    def __init__(self, temp_dir_base=None, run_id=RUN_ID, data_dir=None, destroy="on-success"):
        self._sqllogic_run_id = run_id  # read by _run_id (bypasses _make_run_id)
        self._opts = {
            "--temp-dir-base": temp_dir_base,
            "--data-dir": data_dir,
            "--temp-dir-destroy": destroy,
        }

    def getoption(self, name, default=None):
        return self._opts.get(name, default)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ROOT_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# -----------------------------------------------------------------------------
# URI-scheme classification
#

@pytest.mark.parametrize(
    "value,remote",
    [
        ("/var/tmp/x", False),
        ("relative/path", False),
        ("file:///var/tmp/x", False),
        ("", False),
        (None, False),
        ("s3://bucket/key", True),
        ("abfss://c@acct.dfs.core.windows.net/x", True),
        ("gs://bucket/x", True),
        ("az://container/x", True),
        ("r2://bucket/x", True),
    ],
)
def test_is_remote_root(value, remote):
    assert _is_remote_root(value) is remote


# -----------------------------------------------------------------------------
# Origination — only $BASE + $RUN_ID (+ optional DATA); the binary composes the rest
#

def test_originates_base_and_run_id_only(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base"))
    assert roots["base"] == "/tmp/base"
    assert roots["run_id"] == RUN_ID
    assert roots["destroy"] == "on-success"
    assert roots["data_dir"] is None  # no override → the binary defaults to working_dir/data
    # the driver does NOT compose the four full paths / LOCAL_* here (the binary owns that)
    for wrong_key in _ROOT_VARS:
        assert wrong_key not in roots


def test_unset_base_falls_back_to_system_temp(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base=None))
    assert roots["base"].endswith("ducktest")
    assert roots["base"].startswith(tempfile.gettempdir())
    assert roots["run_id"] == RUN_ID  # run-id is still originated; the binary composes $BASE/$RUN_ID


def test_remote_base_passes_through_verbatim(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="s3://bucket/scratch"))
    assert roots["base"] == "s3://bucket/scratch"  # remote base is the binary's --temp-dir-base as-is
    assert _is_remote_root(roots["base"])
    assert roots["run_id"] == RUN_ID


def test_data_dir_override_is_originated(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base", data_dir="/inputs/data"))
    assert roots["data_dir"] == "/inputs/data"
    # DATA is a plain read-only path: NOT composed with base/run-id
    assert RUN_ID not in roots["data_dir"]
    assert roots["base"] not in roots["data_dir"]


def test_one_run_one_set_cached(clean_env):
    cfg = _Cfg(temp_dir_base="/tmp/base")
    first = _temp_roots(cfg)
    second = _temp_roots(cfg)
    assert first is second  # composed once per run, not re-derived per invocation


# -----------------------------------------------------------------------------
# Passthrough — the corrected flags reach the subprocess; the OLD wrong ones do NOT
#

_ECHO_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    print("ARGV " + " ".join(sys.argv[1:]))
    for k in ("TEMP_DIR", "LOCAL_TEMP_DIR", "DATA_DIR", "LOCAL_DATA_DIR"):
        print("ENV %s=%s" % (k, os.environ.get(k, "<unset>")))
    sys.exit(0)
    """
)


def _echo_stub(tmp_path):
    p = tmp_path / "echo_unittest"
    p.write_text(_ECHO_STUB)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def test_temp_dir_base_and_run_id_reach_subprocess(clean_env, tmp_path):
    base = str(tmp_path / "base")
    roots = _temp_roots(_Cfg(temp_dir_base=base))
    result = _invoke(_echo_stub(tmp_path), ["some/body.test"], str(tmp_path), roots)
    out = result["stdout"]
    # CORRECT flags: --temp-dir-base + --run-id + --temp-dir-destroy passthrough
    assert f"--temp-dir-base {base}" in out
    assert f"--run-id {RUN_ID}" in out
    assert "--temp-dir-destroy on-success" in out
    assert "--emit-test-events" in out
    # WRONG (removed): --temp-dir EXACT is gone (the trailing space avoids matching --temp-dir-base)
    assert "--temp-dir " not in out
    # WRONG (removed): the driver sets NONE of the root env vars — the binary composes/derives them
    for var in _ROOT_VARS:
        assert f"ENV {var}=<unset>" in out


def test_data_dir_passed_only_when_set(clean_env, tmp_path):
    stub = _echo_stub(tmp_path)
    # no override → NO --data-dir (the binary defaults to working_dir/data)
    roots = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base")))
    assert "--data-dir" not in _invoke(stub, ["b.test"], str(tmp_path), roots)["stdout"]
    # explicit override → passed EXACT, never composed with the run-id
    roots2 = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base2"), data_dir="/inputs/data"))
    out = _invoke(stub, ["b.test"], str(tmp_path), roots2)["stdout"]
    assert "--data-dir /inputs/data" in out
    assert f"/inputs/data/{RUN_ID}" not in out


def test_provisioned_env_layers_over_ambient(clean_env, tmp_path):
    roots = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base")))
    result = _invoke(
        _echo_stub(tmp_path), ["some/body.test"], str(tmp_path), roots, env={"DATA_DIR": "s3://override"}
    )
    out = result["stdout"]
    # an explicit per-invocation env (e.g. a provisioned var) still layers over the ambient env
    assert "ENV DATA_DIR=s3://override" in out
    # ...but the driver itself never sets TEMP_DIR (the binary composes it from --temp-dir-base/--run-id)
    assert "ENV TEMP_DIR=<unset>" in out
