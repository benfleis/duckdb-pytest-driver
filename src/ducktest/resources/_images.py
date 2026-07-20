"""ghcr supply chain for the `resources.*` emulator images (azurite, minio, …). See docs/PLAN.md.

The driver publishes each image into an org-controlled ghcr namespace and serves from THAT, so a test
never rides Docker Hub/MCR uptime, rate limits, tag drift, or an upstream that quietly vanishes (as
`duckdb/azurite` did — an intended mirror that was never published).

Buildx-free by design. Publishing uses only core docker (`pull --platform`, `build`, `tag`, `push`,
`manifest`) so it works by hand on any box — amd or arm — with no plugin. Two image KINDS:
  - "mirror": copy an upstream image. `docker pull --platform <p>` fetches ANY arch (we never RUN it, so
     no QEMU), so one machine mirrors every platform.
  - "build":  build a Dockerfile. `docker build` is native/host-arch only, so a build image is published
     one arch per machine (run `--push` on an amd box AND an arm box), then stitched.
A final `--finalize` step assembles the per-arch tags into a multi-arch manifest with `docker manifest`.

Namespace-agnostic. `DUCKTEST_IMAGE_NS` sets where images live; default is the interim personal
`ghcr.io/benfleis` (Ben owns it — no duckdb-org buy-in/SSO to wait on). Migrating to `ghcr.io/duckdb` is a
one env/default change plus a re-push, NOT a code change.

Two env knobs, nothing hard-coded:
  - WHERE images live      -> DUCKTEST_IMAGE_NS      (publish + serve namespace)
  - WHAT a resource RUNS   -> DUCKTEST_IMAGE_SOURCE  ("upstream" | "ghcr")
`DUCKTEST_IMAGE_SOURCE` now defaults to "ghcr" (published 2026-07-18, packages public) — a resource RUNS
the org-controlled ghcr image; set it to "upstream" to fall back to the third-party source. PLAN's
"[c] ghcr as the source".
"""

import os
import platform

IMAGE_NS = os.environ.get("DUCKTEST_IMAGE_NS", "ghcr.io/benfleis").rstrip("/")
IMAGE_SOURCE = os.environ.get("DUCKTEST_IMAGE_SOURCE", "ghcr")  # was "upstream"
DEFAULT_PLATFORMS = ["linux/amd64", "linux/arm64"]

_REGISTRY = {}  # name -> {kind, source, pin, platforms}; resource modules populate it at import

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def host_arch():
    """This machine's docker arch name (amd64/arm64) — the slice a `build` image publishes here."""
    m = platform.machine().lower()
    return _ARCH.get(m, m)


def arch_of(plat):
    """`linux/amd64` -> `amd64` (the docker arch name / per-arch tag suffix)."""
    return plat.split("/")[-1]


def ghcr_ref(name, pin, ns=None):
    """The org-controlled image ref a resource serves from: ``<ns>/ducktest-<name>:<pin>``.

    ``ns`` defaults to :data:`IMAGE_NS`; pass it (e.g. from ``publish-images --namespace``) to target a
    different namespace without touching the env.
    """
    return f"{(ns or IMAGE_NS).rstrip('/')}/ducktest-{name}:{pin}"


def served_image(name, upstream, pin):
    """What a resource actually RUNS: the ghcr image when ``IMAGE_SOURCE == 'ghcr'``, else ``upstream``."""
    return ghcr_ref(name, pin) if IMAGE_SOURCE == "ghcr" else upstream


def register_image(name, kind, source, pin, platforms=None):
    """A resource module declares an image the driver publishes. Idempotent by name (re-import safe).

    ``kind``: ``"mirror"`` (``source`` is an upstream image ref to copy) or ``"build"`` (``source`` is a
    Dockerfile directory to build). ``platforms`` defaults to amd64 + arm64.
    """
    _REGISTRY[name] = {
        "kind": kind,
        "source": source,
        "pin": pin,
        "platforms": list(platforms or DEFAULT_PLATFORMS),
    }


def registry():
    """``[(name, {kind, source, pin, platforms}), …]`` — the single source of truth for publish."""
    return [(name, dict(e)) for name, e in _REGISTRY.items()]
