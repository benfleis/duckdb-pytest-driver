"""Resolve a built binary: --flag > $BUILD_DIR/<path> > build/<variant>/<path> under each place given,
the repository first, then the working directory."""

import functools
import os
import re
import subprocess


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


FLOOR = (2, 0)  # the unittest options ducktest5 gives (--emit-test-events, temp_dir_root, ...) came with v2.0
_VERSION = re.compile(r"\bv(\d+)\.(\d+)\.\d+")


@functools.lru_cache(maxsize=None)
def check_floor(duckdb_bin, unittest_bin=None, scratch=None):
    """Refuses a build older than DuckDB v2.0, by what its shell's `--version` says. A shell that
    does not say leaves it to the unittest binary, when there is one: an older one refuses the
    `--temp-dir-root` option."""
    floor = "v{}.{}".format(*FLOOR)
    said = _said([duckdb_bin, "--version"])
    if m := _VERSION.search(said):
        if (int(m.group(1)), int(m.group(2))) < FLOOR:
            raise UsageError(f"{duckdb_bin} is DuckDB {m.group(0)}; ducktest5 needs DuckDB {floor} or newer")
        return
    if unittest_bin is None:
        raise UsageError(
            f"could not tell the DuckDB version of {duckdb_bin}; ducktest5 needs DuckDB {floor} or newer. "
            f"`--version` gave: {said[:300]!r}"
        )
    os.makedirs(scratch, exist_ok=True)
    if "Unrecognised token" in _said([unittest_bin, "-l", "ducktest5-version-probe", "--temp-dir-root", scratch]):
        raise UsageError(
            f"{unittest_bin} does not take --temp-dir-root: a build older than DuckDB {floor}, which ducktest5 needs"
        )


def _said(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
