"""Self-tests for `auto_init_sql` — the non-`--repl` generalization of `to_init_sql` (RESOURCE-
PLANNING.md §5 phase 9, folded in alongside the suite-matrix work): a bare `.test` item in a suite
registered with `auto_init_sql=True` gets its credentials'/services' `to_init_sql` output run via
upstream's `--init-sqllogic`, instead of hand-writing its own `CREATE SECRET`.

`_suite_init_sql` mirrors `test_repl_init_sql.py`'s pattern exactly (live pytest run, real
service/credential provisioning through the store, no real duckdb binary — the gather function is
called directly). The `.test`-level wiring (`--init-sqllogic` actually reaching the subprocess, and
a matrix cell's `env` reaching it too) is proven against a stub binary, mirroring `test_collector.py`.
"""

import stat
import textwrap

from ducktest.plugin import _write_init_sqllogic_snippet
from ducktest.sqllogic import _matrix_cell_env


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# --- _write_init_sqllogic_snippet (pure) ------------------------------------------------------


def test_write_init_sqllogic_snippet_wraps_one_statement_ok_block(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    path = _write_init_sqllogic_snippet("CREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n")
    text = open(path).read()
    assert text == "statement ok\nCREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n\n"


def test_write_init_sqllogic_snippet_handles_multi_statement_text(tmp_path, monkeypatch):
    # DuckDB's Query() executes a `;`-separated batch in one call -- no per-statement splitting needed.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    sql = "LOAD azure;\nCREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n"
    path = _write_init_sqllogic_snippet(sql)
    assert open(path).read() == f"statement ok\n{sql}\n"


# --- _matrix_cell_env (pure) -------------------------------------------------------------------


class _FakeItem:
    def __init__(self, matrix_cell=None):
        if matrix_cell is not None:
            self._matrix_cell = matrix_cell


def test_matrix_cell_env_none_for_non_matrix_item():
    assert _matrix_cell_env(_FakeItem()) is None


def test_matrix_cell_env_none_when_cell_has_no_env_key():
    assert _matrix_cell_env(_FakeItem(matrix_cell={"backend": "azure-az"})) is None


def test_matrix_cell_env_reads_the_cells_env_key():
    env = {"DATA_DIR": "az://acct.blob.core.windows.net/c"}
    assert _matrix_cell_env(_FakeItem(matrix_cell={"backend": "azure-az", "env": env})) is env


# --- _suite_init_sql (live pytest run, no duckdb binary; mirrors test_repl_init_sql.py) ---------


def test_suite_init_sql_aggregates_credential_and_service(pytester):
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service, credential

        def _start(config):
            return {"endpoint": "svc://up"}

        def _svc_init_sql(block, *, redact=False):
            return f"-- svc secret endpoint={block['endpoint']}\\n"

        def _fetch(config):
            return {"TOKEN": "tok"}

        def _cred_init_sql(value, *, redact=False):
            return f"-- cred secret token={value['TOKEN']}\\n"

        def pytest_configure(config):
            register_suite(config, "s", default=True, auto_init_sql=True,
                services=[service("svc", start=_start, to_init_sql=_svc_init_sql)],
                credentials=[credential("cred", fetch=_fetch, adopt="env", to_init_sql=_cred_init_sql)])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _suite_init_sql
        from ducktest.suites import get_suites

        def test_gather(request):
            suite = get_suites(request.config)[0]
            sql = _suite_init_sql(request.config, suite)
            assert "svc secret endpoint=svc://up" in sql
            assert "cred secret token=tok" in sql
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_suite_init_sql_is_best_effort_on_service_failure():
    # Unlike _repl_resource_init_sql's fail-loud --repl path: no interactive user to explain a
    # failure to, so a service that can't (re-)provision here just contributes nothing.
    from ducktest.plugin import _suite_init_sql
    from ducktest.suites import Suite, service

    class _Cfg:
        pass

    def _start(config):
        raise RuntimeError("docker not found")

    suite = Suite(
        name="s",
        services=(service("svc", start=_start, to_init_sql=lambda block, *, redact=False: "-- unreachable\n"),),
    )
    assert _suite_init_sql(_Cfg(), suite) == ""


# --- end-to-end: --init-sqllogic actually reaches a bare .test subprocess ----------------------

# A stub that records its argv to a file (subprocess.PIPE swallows stdout on a passing test, so
# asserting via captured terminal output doesn't work here -- a file survives regardless of outcome)
# and emits one [TEST_EVENT] end per selected test name, same technique as test_collector.py.
_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys

    def names(argv):
        out = []
        it = iter(argv)
        for a in it:
            if a == "-f":
                path = next(it, None)
                if path:
                    with open(path) as f:
                        out += [ln.strip() for ln in f if ln.strip()]
            elif a.endswith(".test"):
                out.append(a)
        return out

    if "-l" in sys.argv[1:]:
        sys.exit(0)  # the FS-vs-binary reconcile listing call: nothing to register beyond the FS

    with open("argv.log", "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
    for n in names(sys.argv[1:]):
        ev = {"event": "end", "name": n, "status": "ok", "passes": 1, "fails": 0, "skip-mode": 0}
        sys.stderr.write("[TEST_EVENT] " + json.dumps(ev) + "\\n")
    sys.exit(0)
    """
)


def _stub_binary(pytester):
    p = pytester.path / "stub_unittest"
    p.write_text(_STUB)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_bare_test_item_in_auto_init_sql_suite_gets_init_sqllogic_flag(pytester):
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite, credential

        def _fetch(config):
            return {"OK": True}

        def _cred_init_sql(value, *, redact=False):
            return "CREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\\n"

        def pytest_configure(config):
            register_suite(config, "core", path="test/core", auto_init_sql=True,
                credentials=[credential("azure_spn", fetch=_fetch, adopt="env", to_init_sql=_cred_init_sql)])
        """,
    )
    _write(pytester, "test/core/answer.test", "query I\nSELECT 42;\n----\n42\n")
    stub = _stub_binary(pytester)
    result = pytester.runpytest_subprocess("--unittest-binary", str(stub), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    argv = (pytester.path / "argv.log").read_text()
    assert "--init-sqllogic" in argv
    snippet_path = argv.split("--init-sqllogic", 1)[1].split()[0]
    assert "CREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN)" in open(snippet_path).read()


def test_bare_test_item_outside_auto_init_sql_suite_gets_no_flag(pytester):
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite

        def pytest_configure(config):
            register_suite(config, "plain", path="test/plain")
        """,
    )
    _write(pytester, "test/plain/answer.test", "query I\nSELECT 42;\n----\n42\n")
    stub = _stub_binary(pytester)
    result = pytester.runpytest_subprocess("--unittest-binary", str(stub), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert "--init-sqllogic" not in (pytester.path / "argv.log").read_text()
