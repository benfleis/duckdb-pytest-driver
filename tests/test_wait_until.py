"""`wait_until`'s two clocks: the total deadline (backstop) and the stall clock (primary signal).

The distinction exists because a contended service and a wedged one look identical to a bool probe:
both are "not ready yet". A progress token separates them — one keeps moving, the other doesn't.
"""

import time

import pytest

from ducktest.resources._process import wait_until


FAST = {"timeout_s": 5, "poll_s": 0.01}  # generous deadline: only the stall clock should ever fire


def test_bool_probe_ready_returns():
    calls = []

    def probe():
        calls.append(1)
        return len(calls) >= 3

    wait_until(probe, timeout_s=5, on_timeout=lambda: "never", poll_s=0.01)
    assert len(calls) == 3


def test_bool_probe_never_ready_hits_the_deadline():
    with pytest.raises(RuntimeError) as e:
        wait_until(lambda: False, timeout_s=0.05, on_timeout=lambda: "azurite not up", poll_s=0.01)
    msg = str(e.value)
    assert msg.startswith("azurite not up")  # the caller's own message survives
    assert "timed out" in msg and "limit 0.05s" in msg
    assert "stalled" not in msg  # no stall_s given => that clock does not exist


def test_progress_probe_stalls_long_before_the_deadline():
    """The point of the change: die in stall_s, not in timeout_s."""
    seen = ["cmt"]
    polls = []

    def probe():
        polls.append(1)
        if len(polls) == 3:
            seen.append("plain-pending")  # some progress, then nothing ever again
        return tuple(seen)

    started = time.time()
    with pytest.raises(RuntimeError) as e:
        wait_until(probe, on_timeout=lambda: "uc not seeded", stall_s=0.08, **FAST)
    elapsed = time.time() - started

    msg = str(e.value)
    assert "stalled" in msg and "last progress at" in msg
    assert "timed out" not in msg
    assert elapsed < 1  # nowhere near timeout_s=5


def test_progress_probe_that_never_moves_says_so():
    with pytest.raises(RuntimeError) as e:
        wait_until(lambda: "booting", on_timeout=lambda: "uc not seeded", stall_s=0.05, **FAST)
    assert "no progress at all" in str(e.value)


def test_slow_but_advancing_probe_is_not_failed_early():
    """The contention case. Every poll advances, so the stall clock keeps resetting and the run
    survives far longer than stall_s -- which a bool probe could not have distinguished."""
    polls = []

    def probe():
        polls.append(1)
        return True if len(polls) > 25 else len(polls)

    started = time.time()
    wait_until(probe, on_timeout=lambda: "never", stall_s=0.05, **FAST)
    assert time.time() - started > 0.05  # outlived the stall window by advancing
    assert len(polls) == 26


def test_bool_probe_under_a_stall_clock_is_just_a_constant_token():
    """A bool probe carries no progress, so `False` forever reads as a stall -- opt-in via stall_s
    only; the default keeps every existing caller on the deadline alone."""
    with pytest.raises(RuntimeError) as e:
        wait_until(lambda: False, on_timeout=lambda: "minio not up", stall_s=0.05, **FAST)
    assert "no progress at all" in str(e.value)
