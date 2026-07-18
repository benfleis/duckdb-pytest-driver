"""The ghcr resource-image supply chain (ducktest.resources._images + `ducktest publish-images`).

All offline: no docker. Covers the ref/arch math, the registry schema, the source/namespace knobs, and
the CLI verb's per-arch push + finalize command construction (subprocess is captured, never run).
"""

import types

import pytest

from ducktest.cli import _publish_images
from ducktest.resources import _images


def _args(push=False, finalize=False, namespace=None):
    return types.SimpleNamespace(push=push, finalize=finalize, namespace=namespace)


# --- ref / arch / knob math ---------------------------------------------------------------


def test_ghcr_ref_default_and_override():
    assert _images.ghcr_ref("azurite", "2026-07-17") == "ghcr.io/benfleis/ducktest-azurite:2026-07-17"
    assert _images.ghcr_ref("minio", "R1", ns="ghcr.io/duckdb/") == "ghcr.io/duckdb/ducktest-minio:R1"


def test_arch_of():
    assert _images.arch_of("linux/amd64") == "amd64"
    assert _images.arch_of("linux/arm64") == "arm64"


def test_served_image_follows_source_knob(monkeypatch):
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "upstream")
    assert _images.served_image("azurite", "mcr.io/azurite:latest", "P") == "mcr.io/azurite:latest"
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "ghcr")
    assert _images.served_image("azurite", "mcr.io/azurite:latest", "P") == "ghcr.io/benfleis/ducktest-azurite:P"


# --- registry (populated by the resource modules at import) -------------------------------


def test_registry_schema_and_entries():
    from ducktest.resources import azurite, minio  # noqa: F401 — import populates the registry

    reg = dict(_images.registry())
    assert reg["minio"]["kind"] == "mirror"
    assert reg["minio"]["source"].startswith("minio/minio:")
    assert reg["azurite"]["source"].startswith("mcr.microsoft.com/")
    assert reg["minio"]["platforms"] == ["linux/amd64", "linux/arm64"]  # multi-arch by default


def test_resource_image_defaults_are_ghcr_after_flip():
    # Source flipped to "ghcr" (_images.py) once the images were published + made public: a resource
    # now RUNS the org-controlled ghcr ref by default, not the third-party upstream.
    from ducktest.resources import azurite, minio

    assert azurite.IMAGE == azurite.GHCR_IMAGE
    assert minio.IMAGE == minio.GHCR_IMAGE


# --- publish: per-arch push phase ---------------------------------------------------------


def test_push_dry_run_prints_per_arch_plan_no_docker(capsys, monkeypatch):
    called = []
    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda *a, **k: called.append(a))
    rc = _publish_images(_args(push=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert not called  # dry run runs no docker
    # a mirror emits BOTH arch slices from one machine
    assert "ghcr.io/benfleis/ducktest-minio:RELEASE.2025-09-07T16-13-09Z-amd64" in out
    assert "ghcr.io/benfleis/ducktest-minio:RELEASE.2025-09-07T16-13-09Z-arm64" in out
    assert "--finalize" in out  # tells you the next step


def test_push_runs_core_docker_pull_tag_push_per_arch(monkeypatch):
    cmds = []

    class _P:
        returncode = 0

    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda cmd, *a, **k: cmds.append(cmd) or _P())
    rc = _publish_images(_args(push=True))
    assert rc == 0
    # NO buildx anywhere
    assert not any("buildx" in c for cmd in cmds for c in cmd)
    # mirror path is pull --platform / tag / push, per arch
    assert ["docker", "pull", "--platform", "linux/arm64", "minio/minio:RELEASE.2025-09-07T16-13-09Z"] in cmds
    pushes = [c for c in cmds if c[:2] == ["docker", "push"]]
    assert "ghcr.io/benfleis/ducktest-minio:RELEASE.2025-09-07T16-13-09Z-amd64" in [c[-1] for c in pushes]
    assert "ghcr.io/benfleis/ducktest-azurite:2026-07-17-arm64" in [c[-1] for c in pushes]


# --- publish: finalize phase --------------------------------------------------------------


def test_finalize_runs_docker_manifest(monkeypatch):
    cmds = []

    class _P:
        returncode = 0

    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda cmd, *a, **k: cmds.append(cmd) or _P())
    rc = _publish_images(_args(push=True, finalize=True))
    assert rc == 0
    assert not any("buildx" in c for cmd in cmds for c in cmd)
    minio_tgt = "ghcr.io/benfleis/ducktest-minio:RELEASE.2025-09-07T16-13-09Z"
    assert ["docker", "manifest", "create", minio_tgt, minio_tgt + "-amd64", minio_tgt + "-arm64"] in cmds
    assert ["docker", "manifest", "push", minio_tgt] in cmds


# --- build kind (native, host-arch only) --------------------------------------------------


def test_build_kind_publishes_only_host_arch(monkeypatch, capsys):
    _images.register_image("fakebuild", "build", "path/to/dockerdir", "v1")
    monkeypatch.setattr(_images, "host_arch", lambda: "amd64")
    # also patch the name imported into cli's function scope path — it imports host_arch from _images
    monkeypatch.setattr("ducktest.resources._images.host_arch", lambda: "amd64")
    try:
        rc = _publish_images(_args(push=False))
        out = capsys.readouterr().out
        assert rc == 0
        # builds host arch only, natively — no cross-arch, no buildx/QEMU
        assert "docker build -t ghcr.io/benfleis/ducktest-fakebuild:v1-amd64 path/to/dockerdir" in out
        assert "v1-arm64" not in out  # the foreign arch is NOT built here
    finally:
        _images._REGISTRY.pop("fakebuild", None)


# --- namespace agnostic -------------------------------------------------------------------


@pytest.mark.parametrize("ns", ["ghcr.io/benfleis", "ghcr.io/duckdb", "registry.example.com/team"])
def test_namespace_agnostic(ns, capsys):
    _publish_images(_args(push=False, namespace=ns))
    out = capsys.readouterr().out
    assert ("%s/ducktest-minio:" % ns) in out
    assert "ghcr.io/benfleis" == ns or "ghcr.io/benfleis" not in out
