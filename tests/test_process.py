"""Self-test for the shared subprocess-lifecycle helper (``resources/_process.py``).

Offline and service-agnostic: it drives the mechanism with throwaway ``python -c`` children rather than
mitmdump or http.server, so the contract both in-venv services rest on is pinned here without needing
either of them installed.

The ``gone()`` cases carry the weight. An unreaped child becomes a **zombie whose pid still answers**
``os.kill(pid, 0)`` — which is what let an earlier signal-only probe never observe its own child exiting:
every teardown burned the full grace and then SIGKILLed a corpse (2.1s per stop, in both services).
``test_gone_sees_through_an_unreaped_zombie`` asserts the zombie condition itself before probing, so the
test fails loudly if that premise ever stops holding rather than passing for the wrong reason.
"""

import contextlib
import os
import subprocess
import sys
import time

import pytest

from ducktest.resources import _process as process

P = process.Proc("probe", "probe service")

# Writes its own pid to argv[1] (the readiness marker AND how a test recovers the pid after a failed
# launch), prints to stdout (so the log-capture path is observable), then idles until killed.
_CHILD = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
    "print('hello', flush=True); "
    "time.sleep(300)"
)
_SLEEP = "import time; time.sleep(300)"
# Announces that its handler is INSTALLED before idling. Signalling before that point would hit python's
# default SIGTERM handler and kill it — a startup race that silently turns the escalation test green.
_IGNORES_SIGTERM = (
    "import pathlib, signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "pathlib.Path(sys.argv[1]).write_text('armed'); "
    "time.sleep(300)"
)
# Announces itself, then exits immediately — leaving a zombie, since nothing here reaps it.
_EXITS = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('bye')"


@pytest.fixture
def spawn():
    """Spawn tracked children, and make sure a failed assertion can't leak one out of the test."""
    kids = []

    def _spawn(script, *args):
        child = subprocess.Popen([sys.executable, "-c", script, *args], stdout=subprocess.DEVNULL)
        kids.append(child)
        return child

    yield _spawn
    for child in kids:
        with contextlib.suppress(OSError):
            child.kill()
        with contextlib.suppress(Exception):
            child.wait(timeout=5)


def _await_file(path, timeout_s=10.0):
    """Wait for a child's self-announcement. Cheaper and race-free versus guessing at interpreter
    startup time, which is what a bare sleep would be doing."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _await_gone(pid, timeout_s=5.0):
    """Poll until ``pid`` is really gone (SIGKILL isn't synchronous); returns the final verdict."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.gone(pid):
            return True
        time.sleep(0.05)
    return process.gone(pid)


# --- identity + pidfile ----------------------------------------------------------------------------


def test_runtime_filenames_derive_from_the_key(tmp_path):
    assert process.pidfile(P, str(tmp_path)) == os.path.join(str(tmp_path), "probe.pid")
    assert process.logfile(P, str(tmp_path)) == os.path.join(str(tmp_path), "probe.log")


def test_read_pid_tolerates_absent_and_corrupt(tmp_path):
    assert process.read_pid(P, str(tmp_path)) is None  # no pidfile at all

    (tmp_path / "probe.pid").write_text("not-a-pid")
    assert process.read_pid(P, str(tmp_path)) is None  # unparseable => None, not a crash

    (tmp_path / "probe.pid").write_text(" 4242\n")
    assert process.read_pid(P, str(tmp_path)) == 4242  # surrounding whitespace is fine


# --- gone(): the reaping contract ------------------------------------------------------------------


def test_gone_sees_through_an_unreaped_zombie(spawn, tmp_path):
    """THE regression. Nothing here ever calls ``poll()``/``wait()``, so once the child exits it is a
    zombie — and a zombie's pid keeps answering ``os.kill(pid, 0)``. A signal-only probe would therefore
    report it alive forever and this would hang out to the timeout.

    The second assertion is what proves the mechanism rather than the symptom: after ``gone()`` says True
    the pid is no longer waitable, so ``gone()`` itself did the reaping.
    """
    marker = tmp_path / "bye"
    child = spawn(_EXITS, str(marker))
    assert _await_file(marker), "child never ran"

    assert _await_gone(child.pid, 3.0), "gone() must reap our own child, not trust the signal probe"

    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)  # already reaped => gone() is where it happened


def test_gone_is_false_while_our_child_runs(spawn):
    child = spawn(_SLEEP)
    assert process.gone(child.pid) is False


def test_gone_is_true_once_already_reaped(spawn):
    """After an explicit wait() the pid is no longer ours to waitpid — the signal probe answers."""
    child = spawn("pass")
    child.wait(timeout=5)
    assert process.gone(child.pid) is True


def test_gone_is_false_for_a_live_process_that_is_not_ours():
    """pid 1 always exists and is never our child: whether the probe is permitted or refused, the
    answer is 'still alive', never 'gone'."""
    assert process.gone(1) is False


# --- kill() ----------------------------------------------------------------------------------------


def test_kill_returns_promptly_when_sigterm_is_honored(spawn):
    """The 2.1s-per-teardown regression: a cooperative child must not cost the full grace."""
    child = spawn(_SLEEP)
    t0 = time.perf_counter()
    process.kill(child.pid)  # default 2s grace
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.0, f"a SIGTERM-honoring child took {elapsed:.2f}s — the grace is being burned"
    assert process.gone(child.pid) is True


def test_kill_escalates_to_sigkill_when_sigterm_is_ignored(spawn, tmp_path):
    armed = tmp_path / "armed"
    child = spawn(_IGNORES_SIGTERM, str(armed))
    assert _await_file(armed), "child never installed its SIGTERM handler"

    t0 = time.perf_counter()
    process.kill(child.pid, grace_s=0.3)  # short grace so the test isn't slow
    elapsed = time.perf_counter() - t0

    assert elapsed >= 0.3, "it should have waited out the grace before escalating"
    assert _await_gone(child.pid), "SIGKILL should have finished it off"


def test_kill_on_a_dead_pid_is_a_noop(spawn):
    child = spawn("pass")
    child.wait(timeout=5)
    process.kill(child.pid)  # must not raise


# --- sweep() ---------------------------------------------------------------------------------------


def test_sweep_kills_the_tracked_process_and_drops_the_pidfile(spawn, tmp_path):
    control = str(tmp_path)
    child = spawn(_SLEEP)
    (tmp_path / "probe.pid").write_text(str(child.pid))

    process.sweep(P, control)

    assert _await_gone(child.pid)
    assert not os.path.exists(process.pidfile(P, control))


def test_sweep_is_a_noop_without_a_pidfile(tmp_path):
    process.sweep(P, str(tmp_path))  # must not raise
    assert not os.path.exists(process.pidfile(P, str(tmp_path)))


def test_sweep_clears_a_pidfile_whose_process_is_long_gone(spawn, tmp_path):
    """An interrupted run can leave a pidfile for a pid that no longer exists — the stale entry must
    still be cleared, or the next boot reads a bogus handle."""
    control = str(tmp_path)
    child = spawn("pass")
    child.wait(timeout=5)
    (tmp_path / "probe.pid").write_text(str(child.pid))

    process.sweep(P, control)
    assert not os.path.exists(process.pidfile(P, control))


# --- launch() / shutdown() -------------------------------------------------------------------------


def test_launch_tracks_the_child_and_waits_for_ready(tmp_path):
    control, marker = str(tmp_path / "ctl"), tmp_path / "up"
    pid = process.launch(
        P,
        control,
        [sys.executable, "-c", _CHILD, str(marker)],
        ready=marker.exists,
        timeout_s=15,
        where="nowhere",
    )
    try:
        assert marker.exists(), "launch returned before the readiness probe passed"
        assert int(marker.read_text()) == pid  # the pid tracked IS the child's own
        assert process.read_pid(P, control) == pid
    finally:
        process.shutdown(P, control)

    assert _await_gone(pid)
    assert not os.path.exists(process.pidfile(P, control))


def test_launch_captures_child_output_to_the_log(tmp_path):
    control, marker = str(tmp_path / "ctl"), tmp_path / "up"
    process.launch(
        P, control, [sys.executable, "-c", _CHILD, str(marker)], ready=marker.exists, timeout_s=15, where="x"
    )
    try:
        with open(process.logfile(P, control), "rb") as f:
            assert b"hello" in f.read(), "the child's stdout should land in <key>.log"
    finally:
        process.shutdown(P, control)


def test_launch_reclaims_a_leak_from_a_previous_boot(tmp_path):
    """A second launch on the same control dir sweeps the first — what makes a fixed default port safe."""
    control = str(tmp_path / "ctl")
    first_marker, second_marker = tmp_path / "up1", tmp_path / "up2"

    first = process.launch(
        P,
        control,
        [sys.executable, "-c", _CHILD, str(first_marker)],
        ready=first_marker.exists,
        timeout_s=15,
        where="x",
    )
    second = process.launch(
        P,
        control,
        [sys.executable, "-c", _CHILD, str(second_marker)],
        ready=second_marker.exists,
        timeout_s=15,
        where="x",
    )
    try:
        assert second != first
        assert _await_gone(first), "the earlier process should have been reclaimed"
        assert process.read_pid(P, control) == second
    finally:
        process.shutdown(P, control)


def test_launch_tears_down_a_process_that_never_becomes_ready(tmp_path):
    """The half-booted case: `start` raises before its block reaches the store, so the controller's
    teardown would never reach this process — `launch` has to clean up after itself."""
    control, marker = str(tmp_path / "ctl"), tmp_path / "pid"

    with pytest.raises(RuntimeError) as err:
        process.launch(
            P,
            control,
            [sys.executable, "-c", _CHILD, str(marker)],
            ready=lambda: False,  # never ready, whatever the child does
            timeout_s=0.5,
            where="127.0.0.1:9",
        )

    message = str(err.value)
    assert "probe service" in message and "did not become ready" in message
    assert "127.0.0.1:9" in message
    assert "probe.log" in message, "the message should point at the log to look in"

    assert _await_gone(int(marker.read_text())), "a half-booted process must not be left running"
    assert not os.path.exists(process.pidfile(P, control)), "and its pidfile must not be left behind"


# --- wait_until() ----------------------------------------------------------------------------------


def test_wait_until_returns_as_soon_as_the_probe_passes():
    calls = []

    def probe():
        calls.append(1)
        return len(calls) >= 2  # false once, then true

    process.wait_until(probe, 5, lambda: pytest.fail("should not time out"))
    assert len(calls) == 2


def test_wait_until_raises_with_the_lazily_built_message():
    built = []

    def message():
        built.append(1)
        return "nope, never came up"

    with pytest.raises(RuntimeError, match="nope, never came up"):
        process.wait_until(lambda: False, 0.1, message)
    assert len(built) == 1  # built only on the timeout path


def test_wait_until_does_not_build_the_message_on_success():
    process.wait_until(lambda: True, 5, lambda: pytest.fail("message built on the success path"))
