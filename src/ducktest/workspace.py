"""Project-local scratch dir (``<working_dir>/.ducktest/``) for driver-managed service artifacts.

A stable, gitignored home under the repo root for things a service needs to persist across a run —
the mitmproxy CA + its logs today, more later. Distinct from the unittest binary's per-run TEMP_DIR
(``_temp_roots``, which the binary owns and rotates per session): this is the DRIVER's own scratch,
keyed to the repo and REUSED across runs, so e.g. a generated CA is trusted once and kept.
"""

import os

from .context import get_context

SCRATCH_DIR = ".ducktest"


def scratch(config, *parts) -> str:
    """Return ``<working_dir>/.ducktest/<parts...>``, creating it (and parents) if absent.

    ``working_dir`` is the resolved repo root on the :class:`SessionContext`. Call with no parts for
    the scratch root, or with subpath segments for a per-service dir (e.g. ``scratch(config, "mitm")``).
    Stable across runs by design — the path is a pure function of the repo, so a later run finds the
    same dir (how a service locates a pidfile / a reused CA).
    """
    root = os.path.join(get_context(config).working_dir, SCRATCH_DIR, *parts)
    os.makedirs(root, exist_ok=True)
    return root
