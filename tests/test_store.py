"""Self-tests for the process-shared state store (ducktest.store).

Offline, no duckdb/op/docker. Logic is exercised in-process against a real manager
(fast, threads); one test spawns real subprocess workers to prove cross-process
single-flight + eager delivery over the socket (using the default, spawn-safe ctx).
"""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ducktest import store as S

_WORKER = os.path.join(os.path.dirname(__file__), "_store_worker.py")


@pytest.fixture
def server():
    mgr, address, authkey = S.start_server()
    try:
        yield mgr, address, authkey
    finally:
        mgr.shutdown()


@pytest.fixture
def st(server):
    mgr, _address, _authkey = server
    return mgr.store()


# --- eager path -----------------------------------------------------------


def test_copy_returns_present_value(st):
    S.put(st, "creds", {"token": "T"})
    assert S.copy(st, "creds") == {"token": "T"}


def test_copy_is_a_detached_copy(st):
    S.put(st, "creds", {"scopes": ["a"]})
    got = S.copy(st, "creds")
    got["scopes"].append("b")  # mutate my copy
    assert S.copy(st, "creds") == {"scopes": ["a"]}  # shared block unchanged


def test_copy_missing_fails_loud(st):
    with pytest.raises(S.ResourceMissing, match="need creds"):
        S.copy(st, "creds", missing="need creds")


# --- single-flight + locking ----------------------------------------------


def test_single_flight_under_thread_contention(st):
    calls = []
    start = threading.Barrier(5)

    def factory():
        calls.append(1)
        time.sleep(0.1)
        return {"v": 1}

    def worker():
        start.wait()
        return S.copy_or_provision(st, "svc", factory)

    with ThreadPoolExecutor(max_workers=5) as ex:
        results = [f.result() for f in [ex.submit(worker) for _ in range(5)]]

    assert len(calls) == 1  # exactly one provision despite 5 racers
    assert all(r == {"v": 1} for r in results)  # all saw the same block


def test_wait_times_out_on_stuck_owner(st):
    st.begin("k")  # claim PENDING and never terminate -> a stuck/never-finishing owner
    with pytest.raises(S.ProvisionTimeout):
        S.copy_or_provision(st, "k", lambda: {"never": True}, timeout=0.2)


def test_per_key_state_is_independent(st):
    st.begin("a")  # "a" stuck PENDING
    # a different key must not be blocked by "a" being pending
    assert S.copy_or_provision(st, "b", lambda: {"ok": True}, timeout=1.0) == {"ok": True}


def test_poison_pill_fails_fast_without_retry(st):
    def boom():
        raise ValueError("kaboom")

    # the owner sees the original exception (and poisons the key)
    with pytest.raises(ValueError, match="kaboom"):
        S.copy_or_provision(st, "k", boom)
    # every subsequent caller fails fast with ProvisionFailed — the factory is NOT re-run
    calls = []
    with pytest.raises(S.ProvisionFailed, match="kaboom"):
        S.copy_or_provision(st, "k", lambda: calls.append(1) or {"x": 1})
    assert calls == []  # poison pill: no retry storm


# --- env round-trip -------------------------------------------------------


@pytest.mark.parametrize("address", ["/tmp/pymp-abc/sock-123", ("127.0.0.1", 51234)])
def test_env_address_round_trip(address):
    env = S.to_env(address, b"\x00\x01\x02\x03")
    got_addr, got_key = S.from_env(env)
    assert got_addr == address
    assert got_key == b"\x00\x01\x02\x03"


def test_from_env_absent_is_none():
    assert S.from_env({}) is None


# --- cross-process (real subprocess workers over the socket) ---------------


def test_cross_process_single_flight_and_eager_delivery(server, tmp_path):
    mgr, address, authkey = server
    S.put(mgr.store(), "creds", {"token": "EAGER-OK"})  # eager pre-fill, pre-workers

    env = dict(os.environ, **S.to_env(address, authkey))
    procs = [subprocess.Popen([sys.executable, _WORKER, str(tmp_path), str(i)], env=env) for i in range(4)]
    for p in procs:
        assert p.wait(timeout=60) == 0

    results = [json.loads((tmp_path / f"{i}.json").read_text()) for i in range(4)]
    assert len({r["pid"] for r in results}) == 4  # genuinely 4 distinct processes
    assert all(r["creds"] == {"token": "EAGER-OK"} for r in results)  # eager delivery
    assert sum(r["ran_factory"] for r in results) == 1  # single-flight: one provision
    assert len({r["svc"]["pid"] for r in results}) == 1  # all saw the same provisioned block
