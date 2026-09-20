"""Lifecycle for a service that runs as a LOCAL SUBPROCESS rather than a container.

Docker services get this from the daemon for free: ``--name`` is a durable out-of-band handle, ``rm -f``
is an idempotent stop-and-sweep, and ``-d`` means the CLI exits immediately so there is no child to reap.
An in-venv service (mitmproxy, httpserver) has none of that, so this module supplies the equivalents:

* a **pidfile** as the handle — it survives the JSON-block boundary, so ``stop`` needs no stashed ``Popen``
* a **sweep** that reclaims a leak from an interrupted run — the analog of ``docker rm -f`` before boot,
  and what makes a fixed default port safe
* **reaping**, which docker never needs — see :func:`gone`

A service supplies only its identity (:class:`Proc`) and its command; the mechanism lives here, per
``resources/__init__.py``'s contract.
"""

import os
import signal
import subprocess
import time
from collections import namedtuple

from ducktest5.host import wait_until  # noqa: F401  (lives where no pytest is imported)

from ..steps import step

# `key` names the runtime files (`<key>.pid`, `<key>.log`); `label` is what steps call the service.
Proc = namedtuple("Proc", "key label")

_GRACE_S = 2.0  # how long a SIGTERM gets before SIGKILL
_POLL_S = 0.05
def pidfile(proc, control_dir) -> str:
    return os.path.join(control_dir, f"{proc.key}.pid")


def logfile(proc, control_dir) -> str:
    return os.path.join(control_dir, f"{proc.key}.log")


def read_pid(proc, control_dir):
    """The tracked pid, or None if there's no readable pidfile."""
    try:
        with open(pidfile(proc, control_dir)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def gone(pid) -> bool:
    """Whether ``pid`` has really exited — reaping it first if it's OUR child.

    An unreaped child becomes a zombie, and ``os.kill(<zombie>, 0)`` SUCCEEDS, so a signal-only probe
    never observes a child exiting: it would burn the full grace and then SIGKILL a corpse, every time.
    A non-blocking ``waitpid`` reaps ours; ``ChildProcessError`` means it isn't ours (a previous run's
    process), for which the signal probe is the right answer.
    """
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    except ChildProcessError:
        pass  # not our child — fall through to the signal probe
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive, just not ours to signal


def kill(pid, grace_s=_GRACE_S) -> None:
    """SIGTERM, then SIGKILL if it outlives ``grace_s``. A missing process is a no-op."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        time.sleep(_POLL_S)
        if gone(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def sweep(proc, control_dir) -> None:
    """Kill + forget the process in the pidfile. Idempotent, and the same call serves both meanings:
    reclaiming a leak before boot, and stopping the one this run started.

    Timed as a step when a pid is present — the duration distinguishes a clean SIGTERM from a SIGKILL
    escalation — and silent when there's nothing to do, so a clean boot doesn't narrate.
    """
    pid = read_pid(proc, control_dir)
    if pid is not None:
        with step(f"killing tracked {proc.label} (pid {pid})"):
            kill(pid)
    try:
        os.remove(pidfile(proc, control_dir))
    except OSError:
        pass


def launch(proc, control_dir, cmd, *, ready, timeout_s, where, detail="") -> int:
    """Spawn ``cmd`` as a tracked background process in ``control_dir``; wait until ``ready()``; return
    its pid.

    Sweeps a leak first, then writes the pidfile. On a readiness timeout it tears the half-booted process
    down before re-raising: ``start`` raises before its block reaches the store, so the controller's
    end-of-session teardown would never reach it.

    ``where`` (e.g. ``127.0.0.1:18080``) and ``detail`` only shape the messages; ``ready`` is the
    service's own probe.
    """
    os.makedirs(control_dir, exist_ok=True)
    sweep(proc, control_dir)
    log = logfile(proc, control_dir)
    with step(f"starting {proc.label}{f' ({detail})' if detail else ''}"):
        # The child dups the log fd at spawn, so the parent's copy closes with the `with`.
        with open(log, "ab") as logf:
            child = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=logf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group: lifetime is the pidfile's, not the tty's
            )
        with open(pidfile(proc, control_dir), "w") as f:
            f.write(str(child.pid))
        try:
            # Its own step: this is where the time goes and where a bad boot hangs, so it has to be
            # attributable rather than hidden inside the umbrella step above.
            with step(f"waiting for {proc.label} on {where}"):
                wait_until(
                    ready,
                    timeout_s,
                    lambda: f"{proc.label} did not become ready on {where} after {timeout_s}s (see {log}).",
                )
        except Exception:
            sweep(proc, control_dir)
            raise
    return child.pid


def shutdown(proc, control_dir) -> None:
    """Managed teardown (controller, once): stop the tracked process and drop the pidfile."""
    with step(f"stopping {proc.label}"):
        sweep(proc, control_dir)
