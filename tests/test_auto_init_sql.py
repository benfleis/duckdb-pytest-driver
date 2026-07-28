"""Self-tests for `auto_init_sql` — the non-`--repl` generalization of `to_init_sql` (RESOURCE-
PLANNING.md §5 phase 9, folded in alongside the suite-matrix work): a bare `.test` item in a suite
registered with `auto_init_sql=True` gets its credentials'/services' `to_init_sql` output run via
upstream's `--init-sqllogic`, instead of hand-writing its own `CREATE SECRET`.

`_suite_init_sql` mirrors `test_repl_init_sql.py`'s pattern exactly (live pytest run, real
service/credential provisioning through the store, no real duckdb binary — the gather function is
called directly). The `.test`-level wiring (`--init-sqllogic` actually reaching the subprocess, and
a matrix cell's `properties` reaching it too) is proven against a stub binary, mirroring
`test_collector.py`.
"""

import stat
import textwrap

from ducktest.plugin import _write_init_sqllogic_snippet
from ducktest.sqllogic import (
    _matrix_cell_env,
    _matrix_cell_init_sql,
    _matrix_cell_temp_roots,
    _matrix_cell_test_config_args,
)


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# --- _write_init_sqllogic_snippet (pure) ------------------------------------------------------


def test_write_init_sqllogic_snippet_wraps_one_statement_ok_block(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    path = _write_init_sqllogic_snippet("CREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n")
    text = open(path).read()
    assert text == "statement ok\nCREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n"


def test_write_init_sqllogic_snippet_handles_multi_statement_text(tmp_path, monkeypatch):
    # DuckDB's Query() executes a `;`-separated batch in one call -- no per-statement splitting needed.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    sql = "SET foo=1;\nCREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n"
    path = _write_init_sqllogic_snippet(sql)
    assert open(path).read() == f"statement ok\n{sql.rstrip(chr(10))}\n"


def test_write_init_sqllogic_snippet_pulls_require_ahead_of_statement_ok(tmp_path, monkeypatch):
    # `require <ext>` is a directive, not SQL -- it can't sit inside the `statement ok` block, and
    # it's what actually routes through the reliable LoadExtension path (a bare `LOAD azure;`
    # statement only checks $HOME/.duckdb's cache -- see the function's own docstring).
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    sql = "require azure\n\nCREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n"
    path = _write_init_sqllogic_snippet(sql)
    assert open(path).read() == (
        "require azure\n\nstatement ok\nCREATE SECRET az1 (TYPE AZURE, PROVIDER CREDENTIAL_CHAIN);\n"
    )


def test_write_init_sqllogic_snippet_dedupes_and_hoists_requires_from_multiple_credentials(tmp_path, monkeypatch):
    # Two credentials/services both needing "azure" (or different extensions) contribute concatenated
    # text -- require lines can land anywhere in the aggregate, not just at the very start.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    sql = (
        "require azure\n\nCREATE SECRET s1 (TYPE AZURE);\nrequire azure\n\nCREATE SECRET s2 (TYPE AZURE);\n"
        "require ducklake\n\nATTACH 'foo' AS bar (TYPE DUCKLAKE);\n"
    )
    path = _write_init_sqllogic_snippet(sql)
    text = open(path).read()
    assert text.startswith("require azure\n\nrequire ducklake\n\nstatement ok\n")
    assert text.count("require azure") == 1  # deduped, not once per contributing credential
    assert "CREATE SECRET s1 (TYPE AZURE);" in text
    assert "CREATE SECRET s2 (TYPE AZURE);" in text
    assert "ATTACH 'foo' AS bar (TYPE DUCKLAKE);" in text


def test_write_init_sqllogic_snippet_emits_no_statement_ok_when_only_requires(tmp_path, monkeypatch):
    # A to_init_sql that's ONLY "require <ext>\n\n" (no other SQL) must not produce an empty
    # `statement ok` block -- that's a parse error, not a no-op.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    path = _write_init_sqllogic_snippet("require azure\n\n")
    assert open(path).read() == "require azure\n\n"


# --- _matrix_cell_env / _matrix_cell_temp_roots (pure) ------------------------------------------


class _FakeItem:
    def __init__(self, matrix_cell=None):
        if matrix_cell is not None:
            self._matrix_cell = matrix_cell


def test_matrix_cell_env_none_for_non_matrix_item():
    assert _matrix_cell_env(_FakeItem()) is None


def test_matrix_cell_env_none_when_cell_has_no_properties_key():
    assert _matrix_cell_env(_FakeItem(matrix_cell={"backend": "azure-az"})) is None


def test_matrix_cell_env_reads_plain_properties_only():
    # temp_dir_root/data_dir are claimed by _matrix_cell_temp_roots -- not echoed into env too.
    properties = {"AZURE_STORAGE_ACCOUNT": "acct", "temp_dir_root": "az://acct.blob.core.windows.net/w"}
    assert _matrix_cell_env(_FakeItem(matrix_cell={"backend": "azure-az", "properties": properties})) == {
        "AZURE_STORAGE_ACCOUNT": "acct"
    }


def test_matrix_cell_temp_roots_unchanged_for_non_matrix_item():
    base = {"root": "duckdb_unittest_tempdir", "session_id": "s1"}
    assert _matrix_cell_temp_roots(_FakeItem(), base) is base


def test_matrix_cell_temp_roots_overlays_temp_dir_root_and_data_dir():
    base = {"root": "duckdb_unittest_tempdir", "session_id": "s1", "data_dir": None}
    properties = {"temp_dir_root": "az://acct.blob.core.windows.net/w", "data_dir": "az://acct.blob.core.windows.net/d"}
    merged = _matrix_cell_temp_roots(_FakeItem(matrix_cell={"backend": "azure-az", "properties": properties}), base)
    assert merged == {
        "root": "az://acct.blob.core.windows.net/w",
        "session_id": "s1",
        "data_dir": "az://acct.blob.core.windows.net/d",
    }


# --- _matrix_cell_test_config_args (pure) -------------------------------------------------------


def test_test_config_args_empty_for_non_matrix_item():
    assert _matrix_cell_test_config_args(_FakeItem(), "/repo") == []


def test_test_config_args_empty_when_cell_has_no_test_config():
    assert _matrix_cell_test_config_args(_FakeItem(matrix_cell={"backend": "curl"}), "/repo") == []


def test_test_config_args_resolve_relative_path_against_working_dir():
    item = _FakeItem(matrix_cell={"backend": "curl", "properties": {"test_config": "test/configs/httpfs_curl.json"}})
    assert _matrix_cell_test_config_args(item, "/repo") == ["--test-config", "/repo/test/configs/httpfs_curl.json"]


def test_test_config_args_pass_absolute_path_through():
    item = _FakeItem(matrix_cell={"backend": "curl", "properties": {"test_config": "/abs/x.json"}})
    assert _matrix_cell_test_config_args(item, "/repo") == ["--test-config", "/abs/x.json"]


def test_test_config_is_not_leaked_as_an_env_var():
    # test_config is a --test-config path, not an env var: a cell mixing it with a plain var -> env-only.
    cell = {"backend": "curl", "properties": {"test_config": "test/configs/x.json", "S3_ENDPOINT": "minio:9000"}}
    assert _matrix_cell_env(_FakeItem(matrix_cell=cell)) == {"S3_ENDPOINT": "minio:9000"}


# --- _matrix_cell_init_sql (pure) ---------------------------------------------------------------


def test_init_sql_none_for_non_matrix_item():
    assert _matrix_cell_init_sql(_FakeItem()) is None


def test_init_sql_none_when_cell_has_no_init_sql():
    assert _matrix_cell_init_sql(_FakeItem(matrix_cell={"backend": "curl"})) is None


def test_init_sql_read_from_cell():
    cell = {"backend": "curl", "properties": {"init_sql": "SET httpfs_client_implementation='curl';"}}
    assert _matrix_cell_init_sql(_FakeItem(matrix_cell=cell)) == "SET httpfs_client_implementation='curl';"


def test_init_sql_and_test_config_are_not_leaked_as_env_vars():
    # both are claimed by their own mechanisms; a cell mixing them with a plain var -> env-only.
    cell = {"properties": {"init_sql": "SET a=1;", "test_config": "c.json", "S3_ENDPOINT": "minio:9000"}}
    assert _matrix_cell_env(_FakeItem(matrix_cell=cell)) == {"S3_ENDPOINT": "minio:9000"}


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


def test_auto_init_sql_and_plain_suite_items_never_share_a_batch():
    """The actual bug (found live, 2026-07-25, the az azurite/azurite_auth split): with nothing but
    (build, working_dir, cell) as batch affinity, an auto_init_sql suite's item and a plain suite's
    item -- same binary, same working dir, collection-adjacent -- would become BATCH-MATES, sharing
    ONE subprocess invocation. --init-sqllogic is a whole-PROCESS flag, resolved from just the
    batch's representative item, so the plain suite's item would silently inherit the auto_init_sql
    suite's injected secret/SQL too (a real secret leaked exactly this way; see az's
    azurite/azurite_auth split). Fixed by folding the resolved --init-sqllogic argv into
    `_batch_key` (collect.py) -- proven directly at that phase-boundary-struct level (assign_batches'
    own _batch_id output), matching SPEC.md §9's "the struct, not subprocess argv, is the primary
    test surface" -- a batched invocation's test names live in a `-f <tmpfile>` the driver deletes
    once done, not as literal argv tokens, so scraping logged argv text couldn't actually prove this.
    """
    import pathlib

    from ducktest.collect import assign_batches
    from ducktest.suites import register_suite, credential

    class _FakeConfig:
        rootpath = pathlib.Path("/fake/root")

        def getoption(self, name, default=None):
            return default

        def getini(self, name):
            return None

    config = _FakeConfig()
    register_suite(
        config,
        "core",
        path="test/core",
        auto_init_sql=True,
        credentials=[
            credential(
                "azure_spn",
                fetch=lambda cfg: {"OK": True},
                adopt="env",
                to_init_sql=lambda value, *, redact=False: "CREATE SECRET az1 (TYPE AZURE);\n",
            )
        ],
    )
    register_suite(config, "plain", path="test/plain")

    class _FakeItem:
        def __init__(self, path, test_name):
            self.config = config
            self.path = pathlib.Path(path)
            self._binary = "/fake/root/build/debug/test/unittest"
            self._working_dir = "/fake/root"
            self._test_name = test_name

        def add_marker(self, marker):
            pass

        def get_closest_marker(self, name):
            return None

    core_item = _FakeItem("/fake/root/test/core/answer.test", "test/core/answer.test")
    plain_item = _FakeItem("/fake/root/test/plain/answer.test", "test/plain/answer.test")
    assign_batches([core_item, plain_item], batch_size=10)
    assert core_item._batch_id != plain_item._batch_id, "auto_init_sql and plain suite items shared a batch"
