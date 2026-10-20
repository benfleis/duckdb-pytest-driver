"""What the built duckdb a self-test runs against can load, so a test that needs an extension the
build lacks skips with that reason instead of failing on it."""

import functools
import glob
import os
import subprocess


@functools.cache
def extensions(shell):
    """The extensions `shell` has linked in or in its build's own repository."""
    if not shell:
        return frozenset()
    out = subprocess.run(
        [
            shell,
            "-noheader",
            "-csv",
            "-c",
            "SELECT extension_name FROM duckdb_extensions() WHERE install_mode = 'STATICALLY_LINKED'",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "AI_AGENT": ""},
    ).stdout.split()
    repo = glob.glob(os.path.join(os.path.dirname(shell), "repository", "*", "*", "*.duckdb_extension"))
    return frozenset(out) | {os.path.basename(p).split(".", 1)[0] for p in repo}


def missing(shell, *names):
    return [n for n in names if n not in extensions(shell)]
