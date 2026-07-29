"""The ghcr resource-image supply chain (ducktest.resources._images + `ducktest publish-images`).

All offline: no docker. Covers the ref/arch math, the registry schema, the source/namespace knobs, and
the CLI verb's per-arch push + finalize command construction (subprocess is captured, never run).
"""

import os
import types

import pytest

from ducktest.cli import _build_images, _publish_images, _pull_images
from ducktest.resources import _images


def _args(push=False, finalize=False, namespace=None):
    return types.SimpleNamespace(push=push, finalize=finalize, namespace=namespace)


def _pull_args(dry_run=False):
    return types.SimpleNamespace(dry_run=dry_run)


# --- ref / arch / knob math ---------------------------------------------------------------


def test_ghcr_ref_default_and_override():
    assert _images.ghcr_ref("azurite", "2026-07-17") == f"{_images.IMAGE_NS}/ducktest-azurite:2026-07-17"
    assert _images.ghcr_ref("minio", "R1", ns="ghcr.io/duckdb/") == "ghcr.io/duckdb/ducktest-minio:R1"


def test_arch_of():
    assert _images.arch_of("linux/amd64") == "amd64"
    assert _images.arch_of("linux/arm64") == "arm64"


def test_served_image_follows_source_knob(monkeypatch):
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "upstream")
    assert _images.served_image("azurite", "mcr.io/azurite:latest", "P") == "mcr.io/azurite:latest"
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "ghcr")
    assert _images.served_image("azurite", "mcr.io/azurite:latest", "P") == f"{_images.IMAGE_NS}/ducktest-azurite:P"


# --- registry (populated by the resource modules at import) -------------------------------


def test_registry_schema_and_entries():
    from ducktest.resources import azurite, minio  # noqa: F401 — import populates the registry

    reg = dict(_images.registry())
    assert reg["azurite"]["kind"] == "mirror"
    assert reg["azurite"]["source"].startswith("mcr.microsoft.com/")
    assert reg["azurite"]["platforms"] == ["linux/amd64", "linux/arm64"]  # multi-arch by default
    # minio ships our OWN image (mc layered into the server image), so it is a `build`, not a mirror:
    # `source` is the Dockerfile DIR, not an image ref. Azurite carries the mirror assertions above.
    assert reg["minio"]["kind"] == "build"
    assert reg["minio"]["source"].endswith(os.path.join("resources", "images", "minio"))
    assert os.path.isabs(reg["minio"]["source"])  # docker build resolves `source` against the CLI's cwd


def test_resource_image_defaults_are_ghcr_after_flip():
    # Source flipped to "ghcr" (_images.py) once the images were published + made public: a resource
    # now RUNS the org-controlled ghcr ref by default, not the third-party upstream.
    from ducktest.resources import azurite, minio

    assert azurite.IMAGE == azurite.GHCR_IMAGE
    assert minio.IMAGE == minio.GHCR_IMAGE  # a build image serves its ghcr ref the same way


# --- publish: per-arch push phase ---------------------------------------------------------


def test_push_dry_run_prints_per_arch_plan_no_docker(capsys, monkeypatch):
    called = []
    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda *a, **k: called.append(a))
    rc = _publish_images(_args(push=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert not called  # dry run runs no docker
    # a mirror emits BOTH arch slices from one machine
    assert f"{_images.IMAGE_NS}/ducktest-azurite:2026-07-17-amd64" in out
    assert f"{_images.IMAGE_NS}/ducktest-azurite:2026-07-17-arm64" in out
    assert "--finalize" in out  # tells you the next step


def test_push_runs_core_docker_pull_tag_push_per_arch(monkeypatch):
    cmds = []

    class _P:
        returncode = 0

    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda cmd, *a, **k: cmds.append(cmd) or _P())
    rc = _publish_images(_args(push=True))
    assert rc == 0
    # the MIRROR path stays core-docker-only: buildx is reached for `build` kinds and nothing else
    mirror_cmds = [c for c in cmds if any("azurite" in tok for tok in c)]
    assert mirror_cmds and not any("buildx" in tok for c in mirror_cmds for tok in c)
    # mirror path is pull --platform / tag / push, per arch
    upstream = "mcr.microsoft.com/azure-storage/azurite:latest"
    assert ["docker", "pull", "--platform", "linux/arm64", upstream] in cmds
    pushes = [c for c in cmds if c[:2] == ["docker", "push"]]
    assert f"{_images.IMAGE_NS}/ducktest-azurite:2026-07-17-amd64" in [c[-1] for c in pushes]
    assert f"{_images.IMAGE_NS}/ducktest-azurite:2026-07-17-arm64" in [c[-1] for c in pushes]


# --- publish: finalize phase --------------------------------------------------------------


def test_finalize_runs_docker_manifest(monkeypatch):
    cmds = []

    class _P:
        returncode = 0

    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda cmd, *a, **k: cmds.append(cmd) or _P())
    rc = _publish_images(_args(push=True, finalize=True))
    assert rc == 0
    assert not any("buildx" in c for cmd in cmds for c in cmd)
    tgt = f"{_images.IMAGE_NS}/ducktest-azurite:2026-07-17"
    assert ["docker", "manifest", "create", tgt, tgt + "-amd64", tgt + "-arm64"] in cmds
    assert ["docker", "manifest", "push", tgt] in cmds


# --- build kind (native, host-arch only) --------------------------------------------------


def test_build_kind_uses_one_buildx_call_for_every_arch(monkeypatch, capsys):
    """A build image's arches can't be assembled by `pull --platform`, so this is where buildx is used:
    ONE call emits every platform and pushes the manifest itself — no per-arch tags to stitch."""
    _images.register_image("fakebuild", "build", "path/to/dockerdir", "v1")
    monkeypatch.setattr(_images, "host_arch", lambda: "amd64")
    try:
        rc = _publish_images(_args(push=False))
        out = capsys.readouterr().out
        assert rc == 0
        assert (
            "docker buildx build --platform linux/amd64,linux/arm64 --push "
            f"-t {_images.IMAGE_NS}/ducktest-fakebuild:v1 path/to/dockerdir"
        ) in out
        # no per-arch slices for a build: buildx pushes the multi-arch manifest directly
        assert "ducktest-fakebuild:v1-amd64" not in out
    finally:
        _images._REGISTRY.pop("fakebuild", None)


def test_finalize_skips_build_kinds(monkeypatch, capsys):
    """`manifest create` on a build image would reference per-arch tags buildx never pushed."""
    _images.register_image("fakebuild", "build", "path/to/dockerdir", "v1")
    try:
        rc = _publish_images(_args(push=False, finalize=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "skip fakebuild (build kind" in out
        assert f"manifest create {_images.IMAGE_NS}/ducktest-fakebuild" not in out
    finally:
        _images._REGISTRY.pop("fakebuild", None)


# --- build-images: the local dev loop ------------------------------------------------------


def test_build_images_tags_the_local_build_as_the_served_ref(capsys, monkeypatch):
    """The tag IS the point: local and published are interchangeable, so no env override is needed —
    docker runs the local image when it exists and pulls when it doesn't."""
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "ghcr")
    _images.register_image("fakebuild", "build", "path/to/dockerdir", "v1")
    try:
        rc = _build_images(_pull_args(dry_run=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert f"docker build -t {_images.IMAGE_NS}/ducktest-fakebuild:v1 path/to/dockerdir" in out
    finally:
        _images._REGISTRY.pop("fakebuild", None)


def test_build_images_skips_mirrors(capsys):
    """A mirror has nothing to build — pull-images already warms it."""
    rc = _build_images(_pull_args(dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "ducktest-azurite" not in out  # the mirror is not built
    assert "ducktest-minio" in out  # the real build kind is


def test_build_images_runs_no_docker_on_dry_run(monkeypatch, capsys):
    called = []
    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda *a, **k: called.append(a))
    assert _build_images(_pull_args(dry_run=True)) == 0
    capsys.readouterr()
    assert not called


# --- namespace agnostic -------------------------------------------------------------------


@pytest.mark.parametrize("ns", [_images.IMAGE_NS, "ghcr.io/duckdb", "registry.example.com/team"])
def test_namespace_agnostic(ns, capsys):
    _publish_images(_args(push=False, namespace=ns))
    out = capsys.readouterr().out
    assert ("%s/ducktest-minio:" % ns) in out
    # --namespace fully displaces the fallback: nothing may still print the default ns.
    assert _images.IMAGE_NS == ns or _images.IMAGE_NS not in out


# --- pull: warm the served images ---------------------------------------------------------


def test_pull_dry_run_prints_served_refs_no_docker(capsys, monkeypatch):
    called = []
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "ghcr")
    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda *a, **k: called.append(a))
    rc = _pull_images(_pull_args(dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert not called  # dry run runs no docker
    from ducktest.resources import minio

    assert f"docker pull {_images.IMAGE_NS}/ducktest-minio:{minio.PIN}" in out
    assert f"docker pull {_images.IMAGE_NS}/ducktest-azurite:2026-07-17" in out


def test_pull_runs_docker_pull_per_served_image(monkeypatch):
    cmds = []
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "ghcr")
    monkeypatch.setattr(
        "ducktest.cli.subprocess.run",
        lambda a, **k: cmds.append(a) or types.SimpleNamespace(returncode=0),
    )
    rc = _pull_images(_pull_args(dry_run=False))
    assert rc == 0
    pulls = [c for c in cmds if c[:2] == ["docker", "pull"]]
    assert len(pulls) == len(dict(_images.registry()))  # exactly one pull per registered image
    assert all(c[2].startswith(f"{_images.IMAGE_NS}/ducktest-") for c in pulls)  # the ghcr served ref


def test_pull_follows_source_knob_to_upstream(capsys, monkeypatch):
    # source=upstream -> a mirror pulls the third-party ref, not the ghcr one (same knob served_image uses).
    monkeypatch.setattr(_images, "IMAGE_SOURCE", "upstream")
    monkeypatch.setattr("ducktest.cli.subprocess.run", lambda *a, **k: None)
    _pull_images(_pull_args(dry_run=True))
    out = capsys.readouterr().out
    assert "docker pull mcr.microsoft.com/azure-storage/azurite:" in out
    assert _images.IMAGE_NS not in out
