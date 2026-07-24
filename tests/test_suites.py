"""Self-tests for the suite declaration API + registry (register_suite/get_suites, Phase 0).

Phase 0 is INERT: registration records frozen descriptors on `config`; nothing is fetched,
started, or deselected. These exercise the pure registry offline (no real pytest run): a
plain object stands in for `config`, mirroring test_broadcast.py.
"""

import pytest

from ducktest import (
    Credential,
    Service,
    Suite,
    credential,
    get_suites,
    register_suite,
    service,
)


class _Cfg:
    """Minimal stand-in for a pytest Config (an attribute bag)."""


def _load_creds(config):
    return {"TOKEN": "x"}


def _start(config):
    return "started"


def test_empty_config_returns_empty_list():
    assert get_suites(_Cfg()) == []


def test_credential_holds_callables():
    def validate(value):
        return bool(value)

    def error():
        return "no creds"

    cred = credential("dbx", fetch=_load_creds, validate=validate, error=error, adopt="env")
    assert isinstance(cred, Credential)
    assert cred.key == "dbx"
    assert cred.fetch is _load_creds  # held, not called
    assert cred.validate is validate
    assert cred.error is error
    assert cred.adopt == "env"


def test_service_holds_callables():
    def stop(config):
        return None

    svc = service("uc-server", start=_start, stop=stop, fixture="uc_server")
    assert isinstance(svc, Service)
    assert svc.key == "uc-server"
    assert svc.start is _start
    assert svc.stop is stop
    assert svc.fixture == "uc_server"
    # defaults
    bare = service("bare", start=_start)
    assert bare.stop is None
    assert bare.fixture is None


def test_register_and_get_suites_round_trip():
    cfg = _Cfg()
    register_suite(
        cfg,
        "oss_local",
        path="test/oss_local",
        default=True,
        services=[service("oss-uc-server", start=_start, fixture="uc_server")],
    )
    register_suite(
        cfg,
        "databricks",
        path="test/databricks",
        default=False,
        credentials=[credential("databricks_creds", fetch=_load_creds, adopt="env")],
    )

    suites = get_suites(cfg)
    assert [t.name for t in suites] == ["oss_local", "databricks"]  # registration order
    assert all(isinstance(t, Suite) for t in suites)

    oss, dbx = suites
    assert oss.default is True
    assert oss.path == "test/oss_local"
    assert len(oss.services) == 1 and oss.services[0].key == "oss-uc-server"
    assert oss.credentials == ()

    assert dbx.default is False
    assert len(dbx.credentials) == 1 and dbx.credentials[0].fetch is _load_creds
    assert dbx.services == ()


def test_marker_defaults_to_name():
    cfg = _Cfg()
    register_suite(cfg, "databricks", path="test/databricks")
    register_suite(cfg, "smoke", marker="fast")
    by_name = {t.name: t for t in get_suites(cfg)}
    assert by_name["databricks"].marker == "databricks"  # defaulted
    assert by_name["smoke"].marker == "fast"  # explicit wins


def test_no_fetch_or_start_in_phase0():
    cfg = _Cfg()
    calls = []
    register_suite(
        cfg,
        "databricks",
        default=False,
        credentials=[credential("c", fetch=lambda config: calls.append("fetch"))],
        services=[service("s", start=lambda config: calls.append("start"))],
    )
    assert calls == []  # registration is inert


def test_duplicate_name_raises():
    cfg = _Cfg()
    register_suite(cfg, "databricks")
    with pytest.raises(ValueError, match="already registered"):
        register_suite(cfg, "databricks")


def test_credentials_must_be_descriptors():
    cfg = _Cfg()
    with pytest.raises(TypeError, match="credential"):
        register_suite(cfg, "t", credentials=[{"key": "nope"}])


def test_services_must_be_descriptors():
    cfg = _Cfg()
    with pytest.raises(TypeError, match="service"):
        register_suite(cfg, "t", services=["nope"])


def test_credential_shape_validation():
    with pytest.raises(TypeError, match="fetch"):
        credential("k", fetch="not-callable")
    with pytest.raises(ValueError, match="adopt"):
        credential("k", fetch=_load_creds, adopt="secret-store")


def test_service_shape_validation():
    with pytest.raises(TypeError, match="start"):
        service("k", start="not-callable")
    with pytest.raises(TypeError, match="fixture"):
        service("k", start=_start, fixture=123)


def test_to_init_sql_held_and_defaults_none():
    def cred_sql(value, *, redact=False):
        return "-- cred\n"

    def svc_sql(block, *, redact=False):
        return "-- svc\n"

    cred = credential("c", fetch=_load_creds, to_init_sql=cred_sql)
    assert cred.to_init_sql is cred_sql
    assert credential("c2", fetch=_load_creds).to_init_sql is None

    svc = service("s", start=_start, to_init_sql=svc_sql)
    assert svc.to_init_sql is svc_sql
    assert service("s2", start=_start).to_init_sql is None


def test_to_init_sql_shape_validation():
    with pytest.raises(TypeError, match="to_init_sql"):
        credential("c", fetch=_load_creds, to_init_sql="not-callable")
    with pytest.raises(TypeError, match="to_init_sql"):
        service("s", start=_start, to_init_sql="not-callable")


def test_use_service_to_init_sql_override_and_inherit():
    from ducktest import use_service

    def base_sql(block, *, redact=False):
        return "-- base\n"

    def override_sql(block, *, redact=False):
        return "-- override\n"

    base = service("svc", start=_start, to_init_sql=base_sql)
    assert use_service(base).to_init_sql is base_sql  # inherited when not given
    assert use_service(base, to_init_sql=override_sql).to_init_sql is override_sql  # overridden
    assert base.to_init_sql is base_sql  # base untouched
