"""Self-tests for the TEMP/DATA storage-input origination + passthrough (SPEC §11.2/§11.4).

The driver originates ONLY the run's ``root`` + ``session_id`` (the run mnemonic) plus an optional
read-only DATA dir; it composes NO full path in `_temp_roots`. Per invocation, `_invoke` composes the
ONE ``--temp-dir-base = <root>/<session-id>/<batch-id>`` and passes ``--temp-dir-run-id off`` so the
binary appends no run-id level; the binary then adds the ``<test-id>`` leaf, derives ``LOCAL_*``, and
owns local create/reap. These exercise the pure origination (`_temp_roots`) offline and prove the
corrected flags reach the subprocess — and that the OLD wrong passthrough (``--temp-dir`` exact +
``--run-id`` + a ``TEMP_DIR`` env var) does NOT — against a stub binary that echoes its argv/env.
"""

import stat
import textwrap

import pytest

from ducktest.plugin import _is_remote_root, _temp_roots
from ducktest.sqllogic import _invoke, _test_batch_id, item_batch_id

RUN_ID = "2026-07-18T00-00-00Z--brave-fox-42"

# The four env vars the OLD (wrong) driver set; here we assert the driver sets NONE of them (the binary
# composes/derives + overwrites them). The stub echoes each so we can prove it stays unset.
_ROOT_VARS = ("TEMP_DIR", "LOCAL_TEMP_DIR", "DATA_DIR", "LOCAL_DATA_DIR")


class _Cfg:
    """Minimal stand-in for a pytest Config: a fixed session-id + the options `_temp_roots` reads."""

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
# Origination — only root + session_id (+ optional DATA); no full-path composition
#

def test_originates_root_and_session_id_only(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base"))
    assert roots["root"] == "/tmp/base"
    assert roots["session_id"] == RUN_ID
    assert roots["destroy"] == "on-success"
    assert roots["data_dir"] is None  # no override → the binary defaults to working_dir/data
    # the driver does NOT compose full paths / LOCAL_* here (the binary owns that)
    for wrong_key in ("base", "run_id", *_ROOT_VARS):
        assert wrong_key not in roots


def test_unset_base_falls_back_to_binary_default(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base=None))
    # default is the binary's own temp-dir name; the binary resolves it relative to its working dir
    assert roots["root"] == "duckdb_unittest_tempdir"
    assert roots["session_id"] == RUN_ID


def test_remote_base_passes_through_verbatim(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="s3://bucket/scratch"))
    assert roots["root"] == "s3://bucket/scratch"  # remote root is the base as-is
    assert _is_remote_root(roots["root"])
    assert roots["session_id"] == RUN_ID


def test_data_dir_override_is_originated(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base", data_dir="/inputs/data"))
    assert roots["data_dir"] == "/inputs/data"
    # DATA is a plain read-only path: NOT composed with root/session-id
    assert RUN_ID not in roots["data_dir"]
    assert roots["root"] not in roots["data_dir"]


def test_one_run_one_set_cached(clean_env):
    cfg = _Cfg(temp_dir_base="/tmp/base")
    first = _temp_roots(cfg)
    second = _temp_roots(cfg)
    assert first is second  # composed once per run, not re-derived per invocation


# -----------------------------------------------------------------------------
# <batch-id> derivation
#

def test_item_batch_id_batched_vs_single():
    class _It:
        pass

    batched = _It()
    batched._batch_id = 3
    batched._test_name = "test/a.test"
    assert item_batch_id(batched) == "batch-3"

    single = _It()
    single._batch_id = None
    single._test_name = "test/a.test"
    assert item_batch_id(single) == _test_batch_id("test/a.test")
    # a per-test id is stable + distinct across test names
    assert _test_batch_id("test/a.test") != _test_batch_id("test/b.test")


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


def test_composed_base_and_run_id_off_reach_subprocess(clean_env, tmp_path):
    base = str(tmp_path / "base")
    roots = _temp_roots(_Cfg(temp_dir_base=base))
    result = _invoke(_echo_stub(tmp_path), ["some/body.test"], str(tmp_path), roots, batch_id="batch-7")
    out = result["stdout"]
    # CORRECT: the ONE composed --temp-dir-base = <root>/<session-id>/<batch-id> + --temp-dir-run-id off
    assert f"--temp-dir-base {base}/{RUN_ID}/batch-7" in out
    assert "--temp-dir-run-id off" in out
    assert "--temp-dir-destroy on-success" in out
    assert "--emit-test-events" in out
    # WRONG (removed): --run-id is gone
    assert "--run-id" not in out
    # WRONG (removed): --temp-dir EXACT is gone (the trailing space avoids matching --temp-dir-base)
    assert "--temp-dir " not in out
    # WRONG (removed): the driver sets NONE of the root env vars — the binary composes/derives them
    for var in _ROOT_VARS:
        assert f"ENV {var}=<unset>" in out


def test_distinct_batch_ids_yield_distinct_run_roots(clean_env, tmp_path):
    stub = _echo_stub(tmp_path)
    base = str(tmp_path / "base")
    roots = _temp_roots(_Cfg(temp_dir_base=base))
    out7 = _invoke(stub, ["b.test"], str(tmp_path), roots, batch_id="batch-7")["stdout"]
    out8 = _invoke(stub, ["b.test"], str(tmp_path), roots, batch_id="batch-8")["stdout"]
    assert f"--temp-dir-base {base}/{RUN_ID}/batch-7" in out7
    assert f"--temp-dir-base {base}/{RUN_ID}/batch-8" in out8
    assert "batch-7" not in out8 and "batch-8" not in out7  # never collide on the shared session-id


def test_data_dir_passed_only_when_set(clean_env, tmp_path):
    stub = _echo_stub(tmp_path)
    # no override → NO --data-dir (the binary defaults to working_dir/data)
    roots = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base")))
    assert "--data-dir" not in _invoke(stub, ["b.test"], str(tmp_path), roots, batch_id="batch-0")["stdout"]
    # explicit override → passed EXACT, never composed with the session-id
    roots2 = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base2"), data_dir="/inputs/data"))
    out = _invoke(stub, ["b.test"], str(tmp_path), roots2, batch_id="batch-0")["stdout"]
    assert "--data-dir /inputs/data" in out
    assert f"/inputs/data/{RUN_ID}" not in out


def test_provisioned_env_layers_over_ambient(clean_env, tmp_path):
    roots = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base")))
    result = _invoke(
        _echo_stub(tmp_path),
        ["some/body.test"],
        str(tmp_path),
        roots,
        batch_id="batch-0",
        env={"DATA_DIR": "s3://override"},
    )
    out = result["stdout"]
    # an explicit per-invocation env (e.g. a provisioned var) still layers over the ambient env
    assert "ENV DATA_DIR=s3://override" in out
    # ...but the driver itself never sets TEMP_DIR (the binary composes it from --temp-dir-base)
    assert "ENV TEMP_DIR=<unset>" in out
