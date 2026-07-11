"""Self-tests for the tier declaration API + registry (register_tier/get_tiers, Phase 0).

Phase 0 is INERT: registration records frozen descriptors on `config`; nothing is fetched,
started, or deselected. These exercise the pure registry offline (no real pytest run): a
plain object stands in for `config`, mirroring test_broadcast.py.
"""

import pytest

from duckdb_pytest_driver import (
    Credential,
    Service,
    Tier,
    credential,
    get_tiers,
    register_tier,
    service,
)


class _Cfg:
    """Minimal stand-in for a pytest Config (an attribute bag)."""


def _load_creds(config):
    return {"TOKEN": "x"}


def _start(config):
    return "started"


def test_empty_config_returns_empty_list():
    assert get_tiers(_Cfg()) == []


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


def test_register_and_get_tiers_round_trip():
    cfg = _Cfg()
    register_tier(
        cfg,
        "oss_local",
        path="test/oss_local",
        default=True,
        services=[service("oss-uc-server", start=_start, fixture="uc_server")],
    )
    register_tier(
        cfg,
        "databricks",
        path="test/databricks",
        default=False,
        credentials=[credential("databricks_creds", fetch=_load_creds, adopt="env")],
    )

    tiers = get_tiers(cfg)
    assert [t.name for t in tiers] == ["oss_local", "databricks"]  # registration order
    assert all(isinstance(t, Tier) for t in tiers)

    oss, dbx = tiers
    assert oss.default is True
    assert oss.path == "test/oss_local"
    assert len(oss.services) == 1 and oss.services[0].key == "oss-uc-server"
    assert oss.credentials == ()

    assert dbx.default is False
    assert len(dbx.credentials) == 1 and dbx.credentials[0].fetch is _load_creds
    assert dbx.services == ()


def test_marker_defaults_to_name():
    cfg = _Cfg()
    register_tier(cfg, "databricks", path="test/databricks")
    register_tier(cfg, "smoke", marker="fast")
    by_name = {t.name: t for t in get_tiers(cfg)}
    assert by_name["databricks"].marker == "databricks"  # defaulted
    assert by_name["smoke"].marker == "fast"  # explicit wins


def test_no_fetch_or_start_in_phase0():
    cfg = _Cfg()
    calls = []
    register_tier(
        cfg,
        "databricks",
        default=False,
        credentials=[credential("c", fetch=lambda config: calls.append("fetch"))],
        services=[service("s", start=lambda config: calls.append("start"))],
    )
    assert calls == []  # registration is inert


def test_duplicate_name_raises():
    cfg = _Cfg()
    register_tier(cfg, "databricks")
    with pytest.raises(ValueError, match="already registered"):
        register_tier(cfg, "databricks")


def test_credentials_must_be_descriptors():
    cfg = _Cfg()
    with pytest.raises(TypeError, match="credential"):
        register_tier(cfg, "t", credentials=[{"key": "nope"}])


def test_services_must_be_descriptors():
    cfg = _Cfg()
    with pytest.raises(TypeError, match="service"):
        register_tier(cfg, "t", services=["nope"])


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
