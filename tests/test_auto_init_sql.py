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

import json
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


def test_matrix_cell_env_none_for_non_matrix_item():
    assert _matrix_cell_env() is None


def test_matrix_cell_env_none_when_cell_has_no_properties_key():
    assert _matrix_cell_env({"id": "azure-az"}) is None


def test_matrix_cell_env_reads_plain_properties_only():
    # Directory properties are claimed by _matrix_cell_temp_roots -- not echoed into env too.
    properties = {
        "AZURE_STORAGE_ACCOUNT": "acct",
        "temp_dir_root": "az://acct.blob.core.windows.net/w",
        "local_temp_dir_root": "/tmp/az-w",
        "data_dir": "az://acct.blob.core.windows.net/d",
        "local_data_dir": "/data",
    }
    assert _matrix_cell_env({"id": "azure-az", "properties": properties}) == {
        "AZURE_STORAGE_ACCOUNT": "acct"
    }


def test_matrix_cell_temp_roots_unchanged_for_non_matrix_item():
    base = {"root": "duckdb_unittest_tempdir", "session_id": "s1"}
    assert _matrix_cell_temp_roots(None, base) is base


def test_matrix_cell_temp_roots_overlays_remote_and_local_temp_data_dirs():
    base = {
        "root": "duckdb_unittest_tempdir",
        "session_id": "s1",
        "local_temp_dir_root": None,
        "data_dir": None,
        "local_data_dir": None,
    }
    properties = {
        "temp_dir_root": "az://acct.blob.core.windows.net/w",
        "local_temp_dir_root": "/tmp/az-w",
        "data_dir": "az://acct.blob.core.windows.net/d",
        "local_data_dir": "/data",
    }
    merged = _matrix_cell_temp_roots({"id": "azure-az", "properties": properties}, base)
    assert merged == {
        "root": "az://acct.blob.core.windows.net/w",
        "session_id": "s1",
        "local_temp_dir_root": "/tmp/az-w",
        "data_dir": "az://acct.blob.core.windows.net/d",
        "local_data_dir": "/data",
    }


# --- _matrix_cell_test_config_args (pure) -------------------------------------------------------


def test_test_config_args_empty_for_non_matrix_item():
    assert _matrix_cell_test_config_args(None, "/repo") == []


def test_test_config_args_empty_when_cell_has_no_test_config():
    assert _matrix_cell_test_config_args({"id": "curl"}, "/repo") == []


def test_test_config_args_resolve_relative_path_against_working_dir():
    cell = {"id": "curl", "properties": {"test_config": "test/configs/httpfs_curl.json"}}
    assert _matrix_cell_test_config_args(cell, "/repo") == ["--test-config", "/repo/test/configs/httpfs_curl.json"]


def test_test_config_args_pass_absolute_path_through():
    cell = {"id": "curl", "properties": {"test_config": "/abs/x.json"}}
    assert _matrix_cell_test_config_args(cell, "/repo") == ["--test-config", "/abs/x.json"]


def test_test_config_is_not_leaked_as_an_env_var():
    # test_config is a --test-config path, not an env var: a cell mixing it with a plain var -> env-only.
    cell = {"id": "curl", "properties": {"test_config": "test/configs/x.json", "S3_ENDPOINT": "minio:9000"}}
    assert _matrix_cell_env(cell) == {"S3_ENDPOINT": "minio:9000"}


# --- _matrix_cell_init_sql (pure) ---------------------------------------------------------------


def test_init_sql_none_for_non_matrix_item():
    assert _matrix_cell_init_sql() is None


def test_init_sql_none_when_cell_has_no_init_sql():
    assert _matrix_cell_init_sql({"id": "curl"}) is None


def test_init_sql_read_from_cell():
    cell = {"id": "curl", "properties": {"init_sql": "SET httpfs_client_implementation='curl';"}}
    assert _matrix_cell_init_sql(cell) == "SET httpfs_client_implementation='curl';"


def test_init_sql_and_test_config_are_not_leaked_as_env_vars():
    # both are claimed by their own mechanisms; a cell mixing them with a plain var -> env-only.
    cell = {"properties": {"init_sql": "SET a=1;", "test_config": "c.json", "S3_ENDPOINT": "minio:9000"}}
    assert _matrix_cell_env(cell) == {"S3_ENDPOINT": "minio:9000"}


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


def test_auto_init_sql_narrows_to_the_resolved_cells_services(pytester):
    """Two matrix cells of one auto_init_sql suite each narrow to a DIFFERENT service. Cell A's
    --init-sqllogic snippet must contain only its own service's SQL, never cell B's -- the
    auto_init_sql analog of the env-leak tests above, but for the injected SQL snippet."""
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite, service

        def pytest_configure(config):
            register_suite(config, "s", path="test/s", auto_init_sql=True,
                services=[
                    service("svc_a", start=lambda c: {"endpoint": "svc://a"},
                            to_init_sql=lambda b, *, redact=False: "-- cell-a secret\\n"),
                    service("svc_b", start=lambda c: {"endpoint": "svc://b"},
                            to_init_sql=lambda b, *, redact=False: "-- cell-b secret\\n"),
                ],
                matrix=[
                    {"id": "cell-a", "services": ["svc_a"]},
                    {"id": "cell-b", "services": ["svc_b"]},
                ])
        """,
    )
    _write(pytester, "test/s/answer.test", "query I\nSELECT 42;\n----\n42\n")
    stub = _stub_binary(pytester)
    result = pytester.runpytest_subprocess("-n", "0", "--unittest-binary", str(stub), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    lines = (pytester.path / "argv.log").read_text().splitlines()
    assert len(lines) == 2
    snippets = [open(ln.split("--init-sqllogic", 1)[1].split()[0]).read() for ln in lines]
    assert sorted(snippets) == ["statement ok\n-- cell-a secret\n", "statement ok\n-- cell-b secret\n"]


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


def test_run_paired_routes_matrix_config_and_composed_init_sql(pytester, monkeypatch):
    log = pytester.path / "paired-invocations.jsonl"
    monkeypatch.setenv("INVOCATION_LOG", str(log))
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import credential, register_suite

        def _fetch(config):
            return {"OK": True}

        def _credential_sql(value, *, redact=False):
            return "SET suite_setting='suite';\\n"

        def pytest_configure(config):
            register_suite(
                config,
                "paired",
                path="test/paired",
                auto_init_sql=True,
                credentials=[credential("cred", fetch=_fetch, to_init_sql=_credential_sql)],
                matrix=[
                    {"id": "a", "properties": {
                        "CELL": "a",
                        "test_config": "test/configs/a.json",
                        "init_sql": "SET cell_setting='a';",
                    }},
                    {"id": "b", "properties": {
                        "CELL": "b",
                        "test_config": "test/configs/b.json",
                        "init_sql": "SET cell_setting='b';",
                    }},
                ],
            )
        """,
    )
    _write(
        pytester,
        "test/paired/answer.py",
        """
        from ducktest import run_paired

        def test_answer(request, matrix_cell):
            run_paired(request)
        """,
    )
    _write(pytester, "test/paired/answer.test", "query I\nSELECT 42;\n----\n42\n")
    _write(pytester, "test/configs/a.json", "{}")
    _write(pytester, "test/configs/b.json", "{}")
    stub = pytester.path / "stub_unittest"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys

            if "-l" in sys.argv[1:]:
                sys.exit(0)

            def option(name):
                if name not in sys.argv:
                    return None
                return sys.argv[sys.argv.index(name) + 1]

            init_path = option("--init-sqllogic")
            passthrough = [
                sys.argv[index + 1]
                for index, arg in enumerate(sys.argv[:-1])
                if arg == "--env-passthrough"
            ]
            record = {
                "cell": os.environ["CELL"],
                "test_config": option("--test-config"),
                "init_sql": open(init_path).read() if init_path else None,
                "passthrough": passthrough,
            }
            with open(os.environ["INVOCATION_LOG"], "a") as handle:
                handle.write(json.dumps(record) + "\\n")

            name = next(arg for arg in sys.argv[1:] if arg.endswith(".test"))
            event = {"event": "end", "name": name, "status": "ok",
                     "passes": 1, "fails": 0, "skip-mode": 0}
            sys.stderr.write("[TEST_EVENT] " + json.dumps(event) + "\\n")
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = pytester.runpytest_subprocess(
        "--unittest-binary", str(stub), "-p", "no:cacheprovider", "test/paired"
    )

    result.assert_outcomes(passed=2)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert {record["cell"] for record in records} == {"a", "b"}
    for record in records:
        assert record["test_config"] == str(pytester.path / f"test/configs/{record['cell']}.json")
        assert "CELL" in record["passthrough"]
        assert record["init_sql"].index("SET suite_setting='suite';") < record["init_sql"].index(
            f"SET cell_setting='{record['cell']}';"
        )


def test_run_paired_init_sql_wins_and_does_not_leak_across_drivers(pytester, monkeypatch):
    """`run_paired(init_sql=...)` composes after suite init SQL and has final precedence, and two
    paired drivers in the same suite with different `init_sql` don't share a cached snippet (the
    `driver_sql` piece of `_init_sqllogic_arg_for_item`'s memo key)."""
    log = pytester.path / "paired-invocations.jsonl"
    monkeypatch.setenv("INVOCATION_LOG", str(log))
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import credential, register_suite

        def _fetch(config):
            return {"OK": True}

        def _credential_sql(value, *, redact=False):
            return "SET suite_setting='suite';\\n"

        def pytest_configure(config):
            register_suite(
                config,
                "paired",
                path="test/paired",
                auto_init_sql=True,
                credentials=[credential("cred", fetch=_fetch, to_init_sql=_credential_sql)],
            )
        """,
    )
    _write(
        pytester,
        "test/paired/one.py",
        """
        from ducktest import run_paired

        def test_one(request):
            run_paired(request, init_sql="SET driver_setting='one';")
        """,
    )
    _write(pytester, "test/paired/one.test", "query I\nSELECT 1;\n----\n1\n")
    _write(
        pytester,
        "test/paired/two.py",
        """
        from ducktest import run_paired

        def test_two(request):
            run_paired(request, init_sql="SET driver_setting='two';")
        """,
    )
    _write(pytester, "test/paired/two.test", "query I\nSELECT 2;\n----\n2\n")
    stub = pytester.path / "stub_unittest"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys

            if "-l" in sys.argv[1:]:
                sys.exit(0)

            def option(name):
                if name not in sys.argv:
                    return None
                return sys.argv[sys.argv.index(name) + 1]

            init_path = option("--init-sqllogic")
            # The --init-sqllogic snippet is itself a *.test file -- exclude it, or it can be picked
            # up as the test name when it happens to sort ahead of the real one in argv.
            name = next(arg for arg in sys.argv[1:] if arg.endswith(".test") and arg != init_path)
            record = {"name": name, "init_sql": open(init_path).read() if init_path else None}
            with open(os.environ["INVOCATION_LOG"], "a") as handle:
                handle.write(json.dumps(record) + "\\n")

            event = {"event": "end", "name": name, "status": "ok",
                     "passes": 1, "fails": 0, "skip-mode": 0}
            sys.stderr.write("[TEST_EVENT] " + json.dumps(event) + "\\n")
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = pytester.runpytest_subprocess(
        "--unittest-binary", str(stub), "-p", "no:cacheprovider", "test/paired"
    )

    result.assert_outcomes(passed=2)
    records = {json.loads(line)["name"]: json.loads(line)["init_sql"] for line in log.read_text().splitlines()}
    assert set(records) == {"test/paired/one.test", "test/paired/two.test"}
    one, two = records["test/paired/one.test"], records["test/paired/two.test"]
    assert one.index("SET suite_setting='suite';") < one.index("SET driver_setting='one';")
    assert "driver_setting='two'" not in one  # no leakage between distinct driver_sql callers
    assert two.index("SET suite_setting='suite';") < two.index("SET driver_setting='two';")
    assert "driver_setting='one'" not in two


def test_run_paired_isolates_two_functions_driving_the_same_body(pytester, monkeypatch):
    """Two DIFFERENT test functions in one driver, both bare `run_paired(request)` calls against the
    identical same-stem body: `test_name` (the file) and `cell` (none, here) alone would collide on
    the same `--temp-dir-root` -- the pytest node name has to be part of the batch-id seed too."""
    log = pytester.path / "paired-invocations.jsonl"
    monkeypatch.setenv("INVOCATION_LOG", str(log))
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite

        def pytest_configure(config):
            register_suite(config, "paired", path="test/paired")
        """,
    )
    _write(
        pytester,
        "test/paired/answer.py",
        """
        from ducktest import run_paired

        def test_one(request):
            run_paired(request)

        def test_two(request):
            run_paired(request)
        """,
    )
    _write(pytester, "test/paired/answer.test", "query I\nSELECT 42;\n----\n42\n")
    stub = pytester.path / "stub_unittest"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys

            if "-l" in sys.argv[1:]:
                sys.exit(0)

            def option(name):
                if name not in sys.argv:
                    return None
                return sys.argv[sys.argv.index(name) + 1]

            record = {"pid": os.getpid(), "temp_dir_root": option("--temp-dir-root")}
            with open(os.environ["INVOCATION_LOG"], "a") as handle:
                handle.write(json.dumps(record) + "\\n")

            name = next(arg for arg in sys.argv[1:] if arg.endswith(".test"))
            event = {"event": "end", "name": name, "status": "ok",
                     "passes": 1, "fails": 0, "skip-mode": 0}
            sys.stderr.write("[TEST_EVENT] " + json.dumps(event) + "\\n")
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = pytester.runpytest_subprocess(
        "--unittest-binary", str(stub), "-n", "0", "-p", "no:cacheprovider", "test/paired"
    )

    result.assert_outcomes(passed=2)
    roots = [json.loads(line)["temp_dir_root"] for line in log.read_text().splitlines()]
    assert len(roots) == 2
    assert roots[0] != roots[1]


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


def test_suites_sharing_a_matrix_cell_never_share_a_batch_either():
    """Neither suite here is `auto_init_sql` -- httpfs's `s3` and `proxy` suites both use the same
    `S3_CELL` object while `proxy` alone declares a mitmproxy service; without suite identity in the
    batch key, cell-affinity alone would batch them and leak `proxy`'s service env into `s3`'s tests."""
    import pathlib

    from ducktest.collect import assign_batches
    from ducktest.suites import register_suite

    class _FakeConfig:
        rootpath = pathlib.Path("/fake/root")

        def getoption(self, name, default=None):
            return default

        def getini(self, name):
            return None

    config = _FakeConfig()
    register_suite(config, "s3", path="test/s3")
    register_suite(config, "proxy", path="test/proxy")

    class _FakeItem:
        def __init__(self, path, test_name, cell):
            self.config = config
            self.path = pathlib.Path(path)
            self._binary = "/fake/root/build/debug/test/unittest"
            self._working_dir = "/fake/root"
            self._test_name = test_name
            self._cell = cell

        def add_marker(self, marker):
            pass

        def get_closest_marker(self, name):
            return None

    s3_item = _FakeItem("/fake/root/test/s3/one.test", "test/s3/one.test", "s3")
    proxy_item = _FakeItem("/fake/root/test/proxy/two.test", "test/proxy/two.test", "s3")
    assign_batches([s3_item, proxy_item], batch_size=10)
    assert s3_item._batch_id != proxy_item._batch_id, "different suites sharing a cell shared a batch"
