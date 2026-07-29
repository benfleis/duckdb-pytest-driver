"""Shared docker plumbing for the container-backed resource services (azurite, minio, …).

The *mechanism* lives here (per ``resources/__init__.py``'s contract: generic mechanism shared, the
instance stays config). A resource module supplies only its own values — image, ports, the ``alive``
probe, the ready message — and calls this.

Readiness polling is NOT here: ``wait_until`` is generic (a container and a local subprocess both need
it) and lives in :mod:`._process`, the layer with no dependencies of its own.
"""

import subprocess


def docker(*args, check=True):
    """Run ``docker <args>``. On a checked failure, raise with docker's own stderr tail — a bare
    ``CalledProcessError`` hides *why* (e.g. ``docker run`` exit 125 = image unpullable / port in use).
    ``check=False`` (the ``rm -f`` cleanup calls) never raises, so a missing container is a no-op."""
    proc = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = " | ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise RuntimeError(f"docker {args[0]} failed (exit {proc.returncode}): {detail}")
    return proc
