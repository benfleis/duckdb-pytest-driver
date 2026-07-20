"""Self-tests for EXISTING (external) service attach — the --existing-service path (P1).

Three layers, all offline (no docker):
  1. the pure declaration parser (``_parse_existing_services``): grammar + precedence + key norm;
  2. the resource block/derive contracts (``azurite_block``/``azurite_env``, ``minio_block``/``minio_env``);
  3. the attach path end-to-end via an isolated pytest run: a declared-existing service ATTACHES
     (never boots) and a declared-but-dead one FAILS LOUD.
"""

import textwrap

from ducktest.plugin import _norm_service_key, _parse_existing_services
from ducktest.resources.azurite import ACCOUNT, azurite_alive, azurite_block, azurite_env
from ducktest.resources.minio import (
    ACCESS_KEY,
    DEFAULT_BUCKET,
    minio_alive,
    minio_block,
    minio_env,
)


# --- 1. the pure parser -------------------------------------------------------------------


def test_parse_grammar_forms():
    got = _parse_existing_services(["bare", "withurl=http://h:10000/", 'withjson={"account": "acc", "port": 1}'], {})
    assert got["bare"] == {}
    assert got["withurl"] == {"endpoint": "http://h:10000/"}
    assert got["withjson"] == {"account": "acc", "port": 1}


def test_parse_list_splitting_comma_and_semicolon():
    got = _parse_existing_services(["a=http://x,b;c=http://y"], {})
    assert set(got) == {"a", "b", "c"}
    assert got["a"] == {"endpoint": "http://x"} and got["b"] == {} and got["c"] == {"endpoint": "http://y"}


def test_parse_precedence_cli_over_perservice_over_list():
    environ = {
        "DUCKTEST_EXISTING_SERVICES": "svc=http://list",
        "DUCKTEST_EXISTING_SERVICE_SVC": "http://perservice",
    }
    # per-service env beats list env; CLI beats both
    assert _parse_existing_services([], environ)["svc"] == {"endpoint": "http://perservice"}
    assert _parse_existing_services(["svc=http://cli"], environ)["svc"] == {"endpoint": "http://cli"}


def test_parse_env_truthy_and_json():
    environ = {"DUCKTEST_EXISTING_SERVICE_AZURITE": "1", "DUCKTEST_EXISTING_SERVICE_OTHER": '{"port": 9}'}
    got = _parse_existing_services([], environ)
    assert got["azurite"] == {}  # =1 => all defaults
    assert got["other"] == {"port": 9}


def test_parse_key_normalization_dashes_and_case():
    # a dashed service key resolves from an underscore/upper env-var name and vice-versa
    assert _norm_service_key("oss-uc-server") == "oss_uc_server"
    got = _parse_existing_services([], {"DUCKTEST_EXISTING_SERVICE_OSS_UC_SERVER": "1"})
    assert "oss_uc_server" in got
    assert _norm_service_key("OSS-UC-Server") in got


def test_parse_list_env_var_not_swallowed_by_perservice_prefix():
    # DUCKTEST_EXISTING_SERVICES lacks the trailing '_', so it must NOT be read as a per-service entry
    got = _parse_existing_services([], {"DUCKTEST_EXISTING_SERVICES": "a=http://x"})
    assert set(got) == {"a"} and "" not in got


# --- 2. Azurite block/derive --------------------------------------------------------------


def test_azurite_block_defaults():
    b = azurite_block()
    assert b["account"] == ACCOUNT
    assert b["endpoint"] == "http://127.0.0.1:10000"
    assert b["blob_endpoint"] == f"http://127.0.0.1:10000/{ACCOUNT}"
    assert f"BlobEndpoint={b['blob_endpoint']};" in b["connection_string"]


def test_azurite_block_endpoint_override_recomputes_derived_fields():
    b = azurite_block(endpoint="http://host.docker.internal:10000/")
    # derived fields track the override — the whole reason block() is a function, not a dict
    assert b["blob_endpoint"] == f"http://host.docker.internal:10000/{ACCOUNT}"
    assert "host.docker.internal" in b["connection_string"]
    assert "127.0.0.1" not in b["connection_string"]


def test_azurite_block_boot_and_attach_shapes_match():
    # boot returns azurite_block(endpoint=bound); attach returns azurite_block(endpoint=url). Same keys.
    boot = azurite_block(endpoint="http://127.0.0.1:10000")
    attach = azurite_block(endpoint="http://127.0.0.1:10000")
    assert boot == attach


def test_azurite_env_derives_connection_string_and_account():
    env = azurite_env(azurite_block())
    assert env["AZ_STORAGE_ACCOUNT"] == ACCOUNT
    assert env["AZURE_STORAGE_ACCOUNT"] == ACCOUNT
    assert "BlobEndpoint=" in env["AZURE_STORAGE_CONNECTION_STRING"]


def test_azurite_alive_probe(monkeypatch):
    import urllib.error

    calls = {}

    def fake_urlopen(url, timeout=None):
        calls["url"] = url
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)  # server responded => up

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert azurite_alive({"endpoint": "http://127.0.0.1:10000"}) is True
    assert calls["url"] == "http://127.0.0.1:10000"

    def refused(url, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refused)
    assert azurite_alive({"endpoint": "http://127.0.0.1:10000"}) is False


# --- 2b. MinIO's block/derive contract (minio_block / minio_env / minio_alive) ------------


def test_minio_block_defaults():
    b = minio_block()
    assert b["access_key"] == ACCESS_KEY
    assert b["endpoint"] == "http://127.0.0.1:9000"  # scheme'd — rclone + health probe
    assert b["s3_endpoint"] == "127.0.0.1:9000"  # host:port, no scheme — duckdb ENDPOINT
    assert b["url_style"] == "path"
    assert b["use_ssl"] is False
    assert b["bucket"] == DEFAULT_BUCKET


def test_minio_block_endpoint_override_recomputes_s3_endpoint():
    b = minio_block(endpoint="http://host.docker.internal:9000/")
    assert b["endpoint"] == "http://host.docker.internal:9000"  # trailing slash stripped
    assert b["s3_endpoint"] == "host.docker.internal:9000"  # scheme + slash stripped, tracks override
    assert "127.0.0.1" not in b["s3_endpoint"]


def test_minio_block_boot_and_attach_shapes_match():
    boot = minio_block(endpoint="http://127.0.0.1:9000")
    attach = minio_block(endpoint="http://127.0.0.1:9000")
    assert boot == attach


def test_minio_env_maps_block_to_s3_client_vars():
    # Guards every key/value mapping — a mistyped block key or a backwards use_ssl would ship silently
    # (no caller in-repo, and the live tier drives rclone remotes, not this env dict).
    b = minio_block()
    env = minio_env(b)
    assert env["AWS_ACCESS_KEY_ID"] == b["access_key"]
    assert env["AWS_SECRET_ACCESS_KEY"] == b["secret_key"]
    assert env["AWS_REGION"] == b["region"]
    assert env["AWS_ENDPOINT_URL"] == b["endpoint"]  # scheme'd URL for the AWS SDK
    assert env["S3_ENDPOINT"] == b["s3_endpoint"]  # host:port for SET s3_endpoint
    assert env["S3_ACCESS_KEY_ID"] == b["access_key"]
    assert env["S3_SECRET_ACCESS_KEY"] == b["secret_key"]
    assert env["S3_REGION"] == b["region"]
    assert env["S3_URL_STYLE"] == "path"
    assert env["S3_USE_SSL"] == "0"  # False -> "0", not "False"/backwards


def test_minio_alive_probe(monkeypatch):
    import urllib.error

    class _Resp:  # minio_alive uses `with urlopen(...) as r: return r.status == 200`
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = {}

    def ok(url, timeout=None):
        calls["url"] = url
        return _Resp(200)

    monkeypatch.setattr("urllib.request.urlopen", ok)
    assert minio_alive({"endpoint": "http://127.0.0.1:9000"}) is True
    assert calls["url"] == "http://127.0.0.1:9000/minio/health/ready"  # authoritative ready endpoint

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _Resp(503))
    assert minio_alive({"endpoint": "http://127.0.0.1:9000"}) is False  # 503 during startup => not ready

    def refused(url, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refused)
    assert minio_alive({"endpoint": "http://127.0.0.1:9000"}) is False


# --- 3. the attach path end-to-end (isolated pytest run, no docker) -----------------------


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# A fake service whose start() writes a boot log (must stay empty when attaching), whose attach()
# echoes the overrides into a block, and whose alive() is toggled by an env flag.
_CONFTEST = """
    import os
    import pytest
    from ducktest import register_suite, service, get_suites, provision_service

    def _start(config):
        with open(os.environ["BOOT_LOG"], "a") as f:
            f.write("booted\\n")
        return {"endpoint": "svc://booted"}

    def _attach(overrides, config):
        return {"endpoint": overrides.get("endpoint", "svc://default"), "extra": "x"}

    def _alive(block):
        return os.environ.get("ALIVE", "1") == "1"

    def pytest_configure(config):
        register_suite(config, "svc_suite", default=True, services=[
            service("dummy", start=_start, attach=_attach, alive=_alive, fixture="dummy_service")])

    @pytest.fixture(scope="session")
    def dummy_service(request):
        svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "dummy")
        return provision_service(request.config, svc)
"""


def test_existing_service_attaches_and_does_not_boot(pytester, monkeypatch):
    boot_log = pytester.path / "boot.log"
    monkeypatch.setenv("BOOT_LOG", str(boot_log))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        def test_a(dummy_service):
            assert dummy_service["endpoint"] == "svc://external"
            assert dummy_service["attached"] is True
            assert dummy_service["started"] is False
        """,
    )
    result = pytester.runpytest_subprocess(
        "-n", "2", "--existing-service", "dummy=svc://external", "-p", "no:cacheprovider"
    )
    result.assert_outcomes(passed=1)
    assert not boot_log.exists()  # attach => start() never called


def test_existing_service_dead_probe_fails_loud(pytester, monkeypatch):
    monkeypatch.setenv("BOOT_LOG", str(pytester.path / "boot.log"))
    monkeypatch.setenv("ALIVE", "0")  # alive() -> False
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        def test_a(dummy_service):
            assert True
        """,
    )
    result = pytester.runpytest_subprocess(
        "-n", "0", "--existing-service", "dummy=svc://dead", "-p", "no:cacheprovider"
    )
    # attach fails in the session fixture => a loud, red, counted setup error (paradigm shift)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*declared as running at svc://dead*nothing is responding*"])


def test_existing_service_via_env_var(pytester, monkeypatch):
    boot_log = pytester.path / "boot.log"
    monkeypatch.setenv("BOOT_LOG", str(boot_log))
    monkeypatch.setenv("DUCKTEST_EXISTING_SERVICE_DUMMY", "svc://from-env")
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        def test_a(dummy_service):
            assert dummy_service["endpoint"] == "svc://from-env"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not boot_log.exists()
