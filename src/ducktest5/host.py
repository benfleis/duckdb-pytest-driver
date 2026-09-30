"""What a service needs from the host it runs on: the addresses it is published and reached at, the
docker CLI, and waiting until something is up. Nothing here imports pytest."""

import logging
import os
import re
import shlex
import shutil
import subprocess
import time

_log = logging.getLogger(__name__)

_READY_POLL_S = 0.3
_DOCKER_TIMEOUT_S = 180

# A copy of provision.SECRET_NAME: this layer takes no dependency of its own (see wait_until below),
# and this is the only place a secret could reach a command line -- `docker run -e NAME=value`, not env.
_SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSW|CREDENTIAL|_KEY(_ID)?$", re.IGNORECASE)


def _redacted(args):
    """`args`, for the log: the value of a `-e NAME=value` masked when `NAME` looks like a secret."""
    out, next_is_env = [], False
    for a in args:
        if next_is_env:
            name, sep, _ = a.partition("=")
            out.append(f"{name}=***" if sep and _SECRET_NAME.search(name) else a)
        else:
            out.append(a)
        next_is_env = a == "-e"
    return out


def service_bind():
    """Where a service the run starts is published: DUCKTEST_SERVICE_BIND, else 127.0.0.1."""
    return os.environ.get("DUCKTEST_SERVICE_BIND") or "127.0.0.1"


def service_host():
    """Where the run reaches its services: DUCKTEST_SERVICE_HOST, else where they are published,
    and 127.0.0.1 when that is every address. The host that runs docker sets the bind address,
    a machine that only reaches its services sets this one."""
    return os.environ.get("DUCKTEST_SERVICE_HOST") or reached_at(service_bind())


def reached_at(bind):
    """Where something published on `bind` is reached from this machine."""
    return "127.0.0.1" if bind in ("0.0.0.0", "::") else bind


def in_url(address):
    """`address` as the host part of a URL or a `-p` publish: an IPv6 one in brackets."""
    return f"[{address}]" if ":" in address and not address.startswith("[") else address


def has_docker():
    return shutil.which("docker") is not None


def docker(*args, check=True, timeout_s=_DOCKER_TIMEOUT_S):
    """Run ``docker <args>``. On a checked failure, raise with docker's own stderr tail -- a bare
    ``CalledProcessError`` hides *why* (e.g. ``docker run`` exit 125 = image unpullable / port in use).
    ``check=False`` (the ``rm -f`` cleanup calls) never raises, so a missing container, or no docker
    here at all, is a no-op.

    Every call has a timeout: a wedged docker CLI otherwise hangs the run with no output at all.
    """
    _log.debug("docker: %s", shlex.join(_redacted(args)))
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"docker {args[0]} gave no answer in {timeout_s}s") from None
    except FileNotFoundError:
        if check:
            raise RuntimeError(f"docker {args[0]}: there is no docker here (no `docker` on PATH)") from None
        return subprocess.CompletedProcess(["docker", *args], 127, "", "no docker here")
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = " | ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise RuntimeError(f"docker {args[0]} failed (exit {proc.returncode}): {detail}")
    return proc


_NO_PROGRESS = object()


def wait_until(probe, timeout_s, on_timeout, stall_s=None, poll_s=_READY_POLL_S):
    """Poll ``probe()`` until ready, or fail with ``RuntimeError(on_timeout() + <clocks>)``.

    ``probe()`` returns ``True`` when ready. Anything else is a **progress token** for that poll --
    whatever the caller can cheaply observe (a set of seeded schemas, a byte count, a state string).
    A bool probe is the degenerate case: ``False`` is a constant token, i.e. never any progress.

    Two clocks, because "slow" and "wedged" are different failures and only one of them is worth
    waiting out. ``stall_s`` bounds time since the token last *changed*, so a service that is merely
    contended keeps its deadline alive by making progress, while one that dies mid-way fails in
    ``stall_s`` instead of burning the full ``timeout_s``. ``timeout_s`` remains the backstop for
    the case that defeats a stall clock: steady progress that will never actually arrive.

    ``stall_s=None`` (default) disables the stall clock -- pure deadline, the historical behavior, so
    existing bool probes are unaffected. ``on_timeout`` is a callable so the message is built only
    if needed; the clock that fired is appended to it.

    Readiness is generic -- a container and a local subprocess both need it -- so it lives here, the
    layer with no dependencies of its own, and ``_docker`` builds on it.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    token = _NO_PROGRESS
    last_change = start
    advanced = False

    while time.monotonic() < deadline:
        value = probe()
        if value is True:
            return
        now = time.monotonic()
        if value != token:
            advanced = token is not _NO_PROGRESS  # the first token is a baseline, not progress
            token, last_change = value, now
        if stall_s is not None and now - last_change >= stall_s:
            stuck_for = now - last_change
            since = (
                f"last progress at {last_change - start:.1f}s, nothing new for {stuck_for:.1f}s"
                if advanced
                else f"no progress at all in {stuck_for:.1f}s"
            )
            raise RuntimeError(f"{on_timeout()} [stalled: {since}]")
        time.sleep(poll_s)
    raise RuntimeError(f"{on_timeout()} [timed out: {time.monotonic() - start:.1f}s elapsed, limit {timeout_s}s]")
