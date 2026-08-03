import json
import stat
import textwrap

import pytest

from ducktest import env_ref
from ducktest.sqllogic import _env_passthrough_args, _resolve_matrix_properties


def test_env_ref_resolves_static_service_roots_with_matrix_precedence():
    cell = {
        "id": "http",
        "properties": {
            "REMOTE_DATA_URI": env_ref("HTTP_DATA_URL"),
            "HTTP_DATA_URL": "http://matrix.example/data",
            "temp_dir_root": env_ref("HTTP_TEMP_URL_ROOT"),
            "local_temp_dir_root": env_ref("HTTP_TEMP_DIR_ROOT"),
        },
    }
    roots, env = _resolve_matrix_properties(
        {"root": "/default", "session_id": "session-1"},
        cell,
        ambient_env={
            "HTTP_DATA_URL": "http://service.example/data",
            "HTTP_TEMP_URL_ROOT": "http://service.example/temp",
            "HTTP_TEMP_DIR_ROOT": "/srv/temp",
        },
    )

    assert env["HTTP_DATA_URL"] == "http://matrix.example/data"
    assert env["REMOTE_DATA_URI"] == "http://matrix.example/data"
    assert roots["root"] == "http://service.example/temp"
    assert roots["local_temp_dir_root"] == "/srv/temp"


def test_env_ref_can_passthrough_a_same_named_service_variable():
    cell = {
        "id": "s3",
        "properties": {"S3_ACCESS_KEY_ID": env_ref("S3_ACCESS_KEY_ID")},
    }
    _, env = _resolve_matrix_properties(
        {"root": "/default", "session_id": "session-1"},
        cell,
        ambient_env={"S3_ACCESS_KEY_ID": "service-key"},
        supplied_env={"S3_ACCESS_KEY_ID": "caller-key"},
    )

    assert env["S3_ACCESS_KEY_ID"] == "caller-key"


def test_env_ref_missing_variable_names_property_and_matrix_cell():
    cell = {"id": "s3", "properties": {"REMOTE_DATA_URI": env_ref("S3_DATA_URI")}}
    with pytest.raises(
        pytest.UsageError,
        match="matrix cell 's3'.*property 'REMOTE_DATA_URI'.*S3_DATA_URI",
    ):
        _resolve_matrix_properties(
            {"root": "/default", "session_id": "session-1"},
            cell,
            ambient_env={},
        )


def test_env_ref_rejects_empty_name_and_reference_cycles():
    with pytest.raises(ValueError, match="non-empty"):
        env_ref("")

    cell = {
        "id": "cycle",
        "properties": {
            "A": env_ref("B"),
            "B": env_ref("A"),
        },
    }
    with pytest.raises(pytest.UsageError, match="cycle.*A.*B.*A"):
        _resolve_matrix_properties(
            {"root": "/default", "session_id": "session-1"},
            cell,
            ambient_env={},
        )


def test_runner_owned_environment_names_are_not_passed_through():
    _, env = _resolve_matrix_properties(
        {"root": "/default", "session_id": "session-1"},
        None,
        ambient_env={"AMBIENT_ONLY": "ambient"},
        supplied_env={"DATA_DIR": "/caller/data", "CUSTOM_DATA_DIR": "/caller/custom"},
    )
    assert env["DATA_DIR"] == "/caller/data"
    assert "AMBIENT_ONLY" not in env
    assert _env_passthrough_args(env) == ["--env-passthrough", "CUSTOM_DATA_DIR"]


@pytest.mark.parametrize("attached", [False, True])
def test_static_service_roots_and_matrix_refs_reach_each_subprocess(pytester, monkeypatch, attached):
    log = pytester.path / "invocations.jsonl"
    monkeypatch.setenv("INVOCATION_LOG", str(log))
    conftest = pytester.path / "test" / "conftest.py"
    conftest.parent.mkdir(parents=True)
    conftest.write_text(
        textwrap.dedent(
            """
            from ducktest import credential, env_ref, register_suite, service

            def _start(config):
                return {"endpoint": "svc://managed"}

            def _attach(overrides, config):
                return {"endpoint": overrides.get("endpoint", "svc://attached")}

            def _to_env(block):
                return {
                    "SERVICE_DATA_URI": block["endpoint"] + "/data",
                    "SERVICE_TEMP_URI_ROOT": block["endpoint"] + "/temp",
                    "SERVICE_ONLY_VALUE": block["endpoint"] + "/service-only",
                }

            def _fetch_credential(config):
                return {"CREDENTIAL_ONLY_VALUE": "credential-value"}

            def pytest_configure(config):
                register_suite(config, "remote", path="test/remote", credentials=[
                    credential("remote-credential", fetch=_fetch_credential, adopt="env")
                ], services=[
                    service("remote-service", start=_start, attach=_attach, alive=lambda block: True,
                            to_env=_to_env)
                ], matrix=[
                    {"id": "http", "properties": {
                        "MATRIX_SEEN": "http",
                        "REMOTE_DATA_URI": env_ref("SERVICE_DATA_URI"),
                        "temp_dir_root": env_ref("SERVICE_TEMP_URI_ROOT"),
                        "data_dir": env_ref("SERVICE_DATA_URI"),
                    }},
                    {"id": "s3", "properties": {
                        "MATRIX_SEEN": "s3",
                        "REMOTE_DATA_URI": env_ref("SERVICE_DATA_URI"),
                        "temp_dir_root": env_ref("SERVICE_TEMP_URI_ROOT"),
                        "data_dir": env_ref("SERVICE_DATA_URI"),
                    }},
                ])
            """
        )
    )
    body = pytester.path / "test" / "remote" / "answer.test"
    body.parent.mkdir(parents=True)
    body.write_text("query I\nSELECT 42;\n----\n42\n")
    stub = pytester.path / "stub_unittest"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys

            def option(name):
                index = sys.argv.index(name)
                return sys.argv[index + 1]

            passthrough = [
                sys.argv[index + 1]
                for index, arg in enumerate(sys.argv[:-1])
                if arg == "--env-passthrough"
            ]
            record = {
                "data": os.environ["REMOTE_DATA_URI"],
                "data_arg": option("--data-dir"),
                "temp_root": option("--temp-dir-root"),
                "cell": os.environ["MATRIX_SEEN"],
                "service_only": os.environ["SERVICE_ONLY_VALUE"],
                "credential_only": os.environ["CREDENTIAL_ONLY_VALUE"],
                "passthrough": sorted(passthrough),
            }
            with open(os.environ["INVOCATION_LOG"], "a") as f:
                f.write(json.dumps(record) + "\\n")

            names = [arg for arg in sys.argv[1:] if arg.endswith(".test")]
            for name in names:
                event = {"event": "end", "name": name, "status": "ok",
                         "passes": 1, "fails": 0, "skip-mode": 0}
                sys.stderr.write("[TEST_EVENT] " + json.dumps(event) + "\\n")
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    args = ["-p", "no:cacheprovider", "--unittest-binary", str(stub), "test/remote"]
    if attached:
        args[2:2] = ["--existing-service", "remote-service=svc://attached"]
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(passed=2)

    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert {record["cell"] for record in records} == {"http", "s3"}
    endpoint = "svc://attached" if attached else "svc://managed"
    assert {record["data"] for record in records} == {endpoint + "/data"}
    assert {record["service_only"] for record in records} == {endpoint + "/service-only"}
    assert {record["credential_only"] for record in records} == {"credential-value"}
    assert {record["data_arg"] for record in records} == {endpoint + "/data"}
    assert len({record["temp_root"] for record in records}) == 2
    for record in records:
        assert record["temp_root"].startswith(endpoint + "/temp/")
        assert {
            "MATRIX_SEEN",
            "REMOTE_DATA_URI",
            "CREDENTIAL_ONLY_VALUE",
            "SERVICE_DATA_URI",
            "SERVICE_ONLY_VALUE",
            "SERVICE_TEMP_URI_ROOT",
        } <= set(record["passthrough"])
        assert "DATA_DIR" not in record["passthrough"]
        assert "INVOCATION_LOG" not in record["passthrough"]
