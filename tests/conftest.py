# Enable pytest's `pytester` fixture so the collector self-tests can spin up an isolated
# pytest run (in a subprocess) against a stub unittest binary — no real duckdb build needed.
import shutil

import pytest

pytest_plugins = ["pytester"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "docker: opt-in tier that boots REAL docker containers (needs docker + rclone). Skipped by "
        "default; pass `--run-docker` to run it (CI docker job) — see pytest_collection_modifyitems.",
    )


def pytest_addoption(parser):
    # The opt-in switch for the docker resource-bring-up tier. A dedicated flag, NOT `-m docker` /
    # `addopts = -m 'not docker'`: `-m` is single-valued, so any user `-m` would replace the default and
    # silently let real containers boot. A flag has no such interaction — the tier stays off unless asked.
    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        help="run the opt-in `docker` tier that boots real azurite/minio containers (needs docker + rclone)",
    )


def pytest_collection_modifyitems(config, items):
    """Gate every `docker`-marked test behind `--run-docker` (and the tools actually being present).

    Uses `skip` (visible, with a reason) rather than deselection, so `pytest tests/test_resources_live.py`
    tells you *why* nothing ran instead of silently collecting zero. Marker-keyed, so non-docker tests are
    untouched. This is the whole offline-by-default guard — there is no `-m`-based exclusion to defeat.
    """
    if not any("docker" in item.keywords for item in items):
        return
    if not config.getoption("--run-docker"):
        skip = pytest.mark.skip(reason="docker tier is opt-in: pass --run-docker")
    elif not (shutil.which("docker") and shutil.which("rclone")):
        skip = pytest.mark.skip(reason="docker tier needs `docker` and `rclone` on PATH")
    else:
        return  # opted in and tools present — let them run
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)
