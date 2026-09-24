"""Against the real duckdb and unittest binaries: the deliveries that only they can prove.

Set $DUCKDB_SHELL to a built `duckdb`; the unittest binary is `test/unittest` beside it. Without
them these tests are skipped.
"""

import os
import shutil
import textwrap

import pytest

from ducktest5.cli import main


def _shell():
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


def _unittest():
    return os.path.join(os.path.dirname(_shell() or ""), "test", "unittest")


pytestmark = pytest.mark.skipif(
    not (_shell() and os.path.isfile(_unittest())),
    reason="no built duckdb and test/unittest (set $DUCKDB_SHELL)",
)

CONF = """
    from ducktest5 import Variant, default

    AXES = {"client": [
        Variant("httplib", requires=["httpfs"], settings={"httpfs_client_implementation": "httplib"}),
        Variant("curl", requires=["httpfs"], settings={"httpfs_client_implementation": "curl"}),
    ]}
    RULES = [default(variants=["client"])]
"""

BODY = """
    # name: test/client_setting.test
    # group: [probe]

    require httpfs

    query I
    SELECT current_setting('httpfs_client_implementation') = '{CLIENT}';
    ----
    true
"""


def test_a_setting_of_an_extension_reaches_a_test_body(tmp_path, monkeypatch, capsys):
    """The binary applies its own `settings` before any extension is loaded, so this used to fail
    with "options were not recognized". As a SET after the requires it passes."""
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "ducktest_conf.py").write_text(textwrap.dedent(CONF))
    (tmp_path / "test" / "client_setting.test").write_text(textwrap.dedent(BODY).lstrip())
    monkeypatch.chdir(tmp_path)
    code = main(
        ["run", "--duckdb-bin", _shell(), "--unittest-bin", _unittest(), "--no-binary-check", "--temp-dir-root", "runs"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASSED   client_setting.test::client_setting[client=curl]" in out
    assert "PASSED   client_setting.test::client_setting[client=httplib]" in out
