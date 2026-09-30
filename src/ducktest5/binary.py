"""Resolve a built binary: --flag > $BUILD_DIR/<path> > build/<variant>/<path> under each place given,
the repository first, then the working directory."""

import os


class UsageError(Exception):
    pass


def find_built(relative, what, *, flag, explicit=None, build="debug", places=(".",)):
    """The binary at `relative` in a build directory, e.g. `test/unittest`."""
    if explicit:
        candidates = [explicit]
    elif os.environ.get("BUILD_DIR"):
        candidates = [os.path.join(os.environ["BUILD_DIR"], relative)]
    else:
        candidates = [os.path.join(place, "build", build, relative) for place in places]
    candidates = list(dict.fromkeys(os.path.abspath(c) for c in candidates))
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise UsageError(
        f"{what} not found at {' or '.join(candidates)}. Build build/<variant>/{relative} (selected by "
        f"--build / $BUILD_DIR), or pass {flag} PATH."
    )


def find_duckdb(*, explicit=None, build="debug", places=(".",)):
    return find_built("duckdb", "duckdb shell", flag="--duckdb-bin", explicit=explicit, build=build, places=places)
