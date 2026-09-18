"""Resolve the built duckdb shell: --duckdb-bin > $BUILD_DIR/duckdb > build/<variant>/duckdb."""

import os


class UsageError(Exception):
    pass


def find_duckdb(*, explicit=None, build="debug", working_dir="."):
    if explicit:
        path = explicit
    elif os.environ.get("BUILD_DIR"):
        path = os.path.join(os.environ["BUILD_DIR"], "duckdb")
    else:
        path = os.path.join(working_dir, "build", build, "duckdb")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise UsageError(
            f"duckdb shell not found at {path}. Build build/<variant>/duckdb (selected by --build / "
            "$BUILD_DIR), or pass --duckdb-bin PATH."
        )
    return path
