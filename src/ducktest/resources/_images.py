"""ghcr supply chain for the `resources.*` emulator images (azurite, minio, …). See docs/PLAN.md.

The driver publishes each image into a namespace WE control and serves from THAT, so a test never rides
Docker Hub/MCR uptime, rate limits, tag drift, or an upstream that quietly vanishes (as `duckdb/azurite`
did — an intended mirror that was never published).

Two image KINDS, and only one of them needs a plugin:
  - "mirror": copy an upstream image. Core docker only — `pull --platform <p>` fetches ANY arch without
     executing it (so no QEMU), which is why ONE box mirrors every platform, as `…:<pin>-<arch>` slices.
     `--finalize` is what stitches those slices into the multi-arch `…:<pin>` via `docker manifest`.
  - "build":  build a Dockerfile. `docker build` is host-arch only, so this is the one place buildx is
     required: a single `buildx build --platform … --push` emits every arch and pushes its own manifest.
     A build therefore has no per-arch slices, and `--finalize` skips it. Cross-arch is cheap only while
     our Dockerfiles have no RUN steps; one that adds them wants a native runner per arch instead.

Namespace-agnostic. `DUCKTEST_IMAGE_NS` sets where images live; the fallback is :data:`DEFAULT_IMAGE_NS`,
an interim personal namespace (no duckdb-org buy-in/SSO to wait on). That constant is the ONE place the
namespace is written down — the CLI's help and the tests read it rather than repeating the string — so
migrating to `ghcr.io/duckdb` is a one-line change plus a re-push, NOT a code change.

Two env knobs, nothing hard-coded:
  - WHERE images live      -> DUCKTEST_IMAGE_NS      (publish + serve namespace)
  - WHAT a resource RUNS   -> DUCKTEST_IMAGE_SOURCE  ("upstream" | "ghcr")
`DUCKTEST_IMAGE_SOURCE` defaults to "ghcr" — a resource RUNS the ref we publish, not the third-party one;
set it to "upstream" to fall back. PLAN's "[c] ghcr as the source". Note what the two knobs imply
together: pointing `DUCKTEST_IMAGE_NS` at a namespace also commits you to publishing there AND flipping
each package public, or the default pull is an anonymous denial against an empty namespace.
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
    """This machine's docker arch name (amd64/arm64).

    Unreferenced since a `build` stopped publishing a host-arch slice (one buildx call now emits every
    arch); kept as the arch probe any native-per-arch path would need back.
    """
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
