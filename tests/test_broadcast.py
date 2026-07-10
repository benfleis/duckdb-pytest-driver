"""Self-tests for the controller->worker broadcast seam (register_broadcast/get_broadcast).

An invocation-level value is computed ONCE on the controller (via a registered factory) and handed
to every xdist worker through `workerinput`. These exercise the pure logic offline (no real xdist):
a plain object stands in for `config` -- controller = no `workerinput`, worker = a `workerinput`
dict -- mirroring what pytest/xdist provide.
"""

import pytest

from duckdb_pytest_driver import get_broadcast, register_broadcast


class _Cfg:
    """Minimal stand-in for a pytest Config (an attribute bag)."""


def test_controller_computes_once_and_caches():
    cfg = _Cfg()
    calls = []

    def factory(config):
        calls.append(config)
        return {"A": "1"}

    register_broadcast(cfg, "creds", factory)
    assert get_broadcast(cfg, "creds") == {"A": "1"}
    assert get_broadcast(cfg, "creds") == {"A": "1"}  # cached
    assert len(calls) == 1  # factory ran exactly once
    assert calls[0] is cfg  # factory receives the config


def test_worker_reads_broadcast_never_runs_factory():
    cfg = _Cfg()
    cfg.workerinput = {"creds": {"A": "from-controller"}}

    def factory(config):
        raise AssertionError("a worker must not run the factory")

    register_broadcast(cfg, "creds", factory)  # no-op on a worker
    assert get_broadcast(cfg, "creds") == {"A": "from-controller"}


@pytest.mark.parametrize("default,expected", [({}, {}), (None, None), ("x", "x")])
def test_default_when_unregistered(default, expected):
    assert get_broadcast(_Cfg(), "missing", default) == expected
