"""Self-tests for the TEMP/DATA storage-root origination + passthrough (SPEC §11.3).

The driver ORIGINATES all four roots once per run (mnemonic-tagged), derives each guaranteed-local
sibling by URI-scheme classification, and passes all four to EVERY unittest invocation as env vars
plus ``--temp-dir`` EXACT (never ``--temp-dir-base``). These exercise the pure composition
(`_temp_roots`) offline and prove the four vars + exact temp dir reach the subprocess, against a
stub binary that echoes its argv/env.
"""

import os
import stat
import textwrap

import pytest

from ducktest.plugin import _is_remote_root, _temp_roots
from ducktest.sqllogic import _invoke

RUN_ID = "2026-07-18T00-00-00Z--brave-fox-42"
MNEM = "brave-fox-42"

_ROOT_VARS = ("TEMP_DIR", "LOCAL_TEMP_DIR", "DATA_DIR", "LOCAL_DATA_DIR")


class _Cfg:
    """Minimal stand-in for a pytest Config: a fixed run-id + the --temp-dir-base option."""

    def __init__(self, temp_dir_base=None, run_id=RUN_ID):
        self._temp_dir_base = temp_dir_base
        self._sqllogic_run_id = run_id  # read by _run_id (bypasses _make_run_id)

    def getoption(self, name, default=None):
        if name == "--temp-dir-base":
            return self._temp_dir_base
        return default


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
# Origination — unset roots default local + mnemonic'd
#

def test_unset_roots_are_local_and_mnemonicd(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base"))
    run_local = os.path.join("/tmp/base", RUN_ID)
    assert roots["TEMP_DIR"] == os.path.join(run_local, "temp")
    assert roots["DATA_DIR"] == os.path.join(run_local, "data")
    # local root -> LOCAL_X is the SAME dir (§11.2: LOCAL_X = X when X is local)
    assert roots["LOCAL_TEMP_DIR"] == roots["TEMP_DIR"]
    assert roots["LOCAL_DATA_DIR"] == roots["DATA_DIR"]
    # tagged with the run mnemonic, not a pid; temp and data axes stay distinct
    assert all(MNEM in v for v in roots.values())
    assert roots["TEMP_DIR"] != roots["DATA_DIR"]


def test_unset_base_falls_back_to_system_temp(clean_env):
    roots = _temp_roots(_Cfg(temp_dir_base=None))
    assert RUN_ID in roots["TEMP_DIR"] and roots["TEMP_DIR"].endswith(os.path.join(RUN_ID, "temp"))


# -----------------------------------------------------------------------------
# Origination — remote root gets a distinct guaranteed-local sibling
#

def test_remote_root_gets_distinct_local_sibling(clean_env):
    clean_env.setenv("TEMP_DIR", "s3://bucket/scratch")
    clean_env.setenv("DATA_DIR", "s3://bucket/data")
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base"))
    run_local = os.path.join("/tmp/base", RUN_ID)
    # the remote root passes through verbatim...
    assert roots["TEMP_DIR"] == "s3://bucket/scratch"
    assert roots["DATA_DIR"] == "s3://bucket/data"
    # ...while its LOCAL sibling is a guaranteed-local, mnemonic'd dir (NOT the remote value)
    assert roots["LOCAL_TEMP_DIR"] == os.path.join(run_local, "temp")
    assert roots["LOCAL_DATA_DIR"] == os.path.join(run_local, "data")
    assert not _is_remote_root(roots["LOCAL_TEMP_DIR"])


def test_explicit_local_sibling_wins(clean_env):
    clean_env.setenv("TEMP_DIR", "s3://bucket/scratch")
    clean_env.setenv("LOCAL_TEMP_DIR", "/my/fast/disk")
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base"))
    assert roots["TEMP_DIR"] == "s3://bucket/scratch"
    assert roots["LOCAL_TEMP_DIR"] == "/my/fast/disk"  # user-set value wins over derivation


def test_explicit_root_wins_over_default(clean_env):
    clean_env.setenv("TEMP_DIR", "/somewhere/else")
    roots = _temp_roots(_Cfg(temp_dir_base="/tmp/base"))
    assert roots["TEMP_DIR"] == "/somewhere/else"
    assert roots["LOCAL_TEMP_DIR"] == "/somewhere/else"  # local -> LOCAL_X == X


# -----------------------------------------------------------------------------
# One run -> one set (composed once, cached, identical across invocations)
#

def test_one_run_one_set_cached(clean_env):
    cfg = _Cfg(temp_dir_base="/tmp/base")
    first = _temp_roots(cfg)
    second = _temp_roots(cfg)
    assert first is second  # composed once per run, not re-derived per invocation


# -----------------------------------------------------------------------------
# Passthrough — all four env vars + --temp-dir EXACT reach the subprocess
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


def test_all_four_passed_to_subprocess_and_temp_dir_exact(clean_env, tmp_path):
    roots = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base")))
    result = _invoke(_echo_stub(tmp_path), ["some/body.test"], str(tmp_path), roots)
    out = result["stdout"]
    # every root delivered as an env var the binary's resolver reads
    for key, value in roots.items():
        assert f"ENV {key}={value}" in out
    # --temp-dir passed EXACT (the local scratch), never --temp-dir-base
    assert f"--temp-dir {roots['LOCAL_TEMP_DIR']}" in out
    assert "--temp-dir-base" not in out
    assert "--emit-test-events" in out


def test_provisioned_env_layers_over_roots(clean_env, tmp_path):
    roots = _temp_roots(_Cfg(temp_dir_base=str(tmp_path / "base")))
    result = _invoke(
        _echo_stub(tmp_path), ["some/body.test"], str(tmp_path), roots, env={"DATA_DIR": "s3://override"}
    )
    out = result["stdout"]
    assert "ENV DATA_DIR=s3://override" in out  # explicit per-invocation env wins over the root
    assert f"ENV TEMP_DIR={roots['TEMP_DIR']}" in out
