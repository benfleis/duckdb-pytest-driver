"""A docker CLI that never answers must fail the call, not hang the session."""

import os
import stat

import pytest

from ducktest.resources._docker import docker


@pytest.fixture
def wedged_docker(tmp_path, monkeypatch):
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_a_wedged_docker_call_fails_with_a_timeout(wedged_docker):
    with pytest.raises(RuntimeError, match=r"docker ps gave no answer in 0\.2s"):
        docker("ps", timeout_s=0.2)


def test_the_cleanup_form_times_out_too(wedged_docker):
    with pytest.raises(RuntimeError, match="gave no answer"):
        docker("rm", "-f", "x", check=False, timeout_s=0.2)
