import os

import pytest

# What the shell running the self-tests holds that an SDK would read: tests pin the runner's view of
# the shell's env, so a developer's own AWS_PROFILE must not reach them.
_SDK_PREFIXES = ("AWS_", "AZURE_", "IDENTITY_", "MSI_", "IMDS_", "GOOGLE_")


@pytest.fixture(autouse=True)
def _no_sdk_env_from_the_shell(monkeypatch):
    for name in [n for n in os.environ if n.startswith(_SDK_PREFIXES)]:
        monkeypatch.delenv(name)
