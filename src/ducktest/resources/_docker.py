"""Shared docker plumbing for the container-backed resource services (azurite, minio, …).

The *mechanism* lives here (per ``resources/__init__.py``'s contract: generic mechanism shared, the
instance stays config). A resource module supplies only its own values — image, ports, the ``alive``
probe, the ready message — and calls this.

Readiness polling is NOT here: ``wait_until`` is generic (a container and a local subprocess both need
it) and lives in :mod:`._process`, the layer with no dependencies of its own.
"""

from ducktest5.host import docker  # noqa: F401  (lives where no pytest is imported)
