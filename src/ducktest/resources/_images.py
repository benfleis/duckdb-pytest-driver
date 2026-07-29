"""Image registry metadata for resource emulators.

Resources declare images here so the CLI can publish, build, and pull them from one registry.

Kinds:
  - "mirror": copy an upstream image. Core docker is enough: `pull --platform <p>` fetches a platform
    without executing it, then `--finalize` stitches the per-arch tags with `docker manifest`.
  - "build": build a Dockerfile. `publish-images` uses one buildx call for all platforms; build images
    have no per-arch tags and are skipped by `--finalize`.

Environment:
  - DUCKTEST_IMAGE_NS: registry namespace for publish and serve
  - DUCKTEST_IMAGE_SOURCE: "ghcr" to run the published ref, "upstream" to run the source image
"""

import os
import platform

DEFAULT_IMAGE_NS = "ghcr.io/hendriklambert"  # lowercase: ghcr rejects mixed-case repository names
IMAGE_NS = os.environ.get("DUCKTEST_IMAGE_NS", DEFAULT_IMAGE_NS).rstrip("/")
IMAGE_SOURCE = os.environ.get("DUCKTEST_IMAGE_SOURCE", "ghcr")  # was "upstream"
DEFAULT_PLATFORMS = ["linux/amd64", "linux/arm64"]

_REGISTRY = {}  # name -> {kind, source, pin, platforms}; resource modules populate it at import

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def host_arch():
    """This machine's docker arch name (amd64/arm64)."""
    m = platform.machine().lower()
    return _ARCH.get(m, m)


def arch_of(plat):
    """`linux/amd64` -> `amd64` (the docker arch name / per-arch tag suffix)."""
    return plat.split("/")[-1]


def ghcr_ref(name, pin, ns=None):
    """The published image ref a resource serves from: ``<ns>/ducktest-<name>:<pin>``.

    ``ns`` defaults to :data:`IMAGE_NS`; pass it (e.g. from ``publish-images --namespace``) to target a
    different namespace without touching the env.
    """
    return f"{(ns or IMAGE_NS).rstrip('/')}/ducktest-{name}:{pin}"


def served_image(name, upstream, pin):
    """Image ref a resource runs."""
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
