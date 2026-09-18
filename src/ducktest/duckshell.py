"""A thin, stateless SQL runner over the built duckdb SHELL (the one :func:`find_duckdb`
resolves) -- for pure-Python tests that need real connections instead of the `.test` lane.

"shell" is duckdb's own term for its command-line tool; we use it (not "CLI") to avoid
conflation with pytest's CLI.

Using the built shell (not pip `duckdb`) is version-safe against a locally-built extension.
Each op spawns a fresh process running ``preamble + sql``, so it always sees the latest
committed state and a :class:`DuckShell` is safe to share across threads. Reads use
``duckdb -json`` and are parsed to ``list[dict]``; `conflict_markers` classify a retryable
write conflict so :meth:`DuckShell.commit` can report it rather than raise.

Consumer-specific bits (which extensions to LOAD, secrets, ATTACH, the conflict wording)
live in the `preamble` + `conflict_markers` you pass -- keep this module backend-neutral.

    db = connect_shell(config, preamble)         # preamble: str, or callable(build_dir)->str
    db.column("SELECT id FROM t ORDER BY id")    # -> [1, 2, 3]
    db.commit("INSERT ...")                      # True=committed / False=conflict (if markers set)
"""

import os

from ducktest5.shell import DuckShell, last_json_array  # noqa: F401

from .plugin import find_duckdb


def connect_shell(config, preamble, *, conflict_markers=(), working_dir=None, timeout=60):
    """Resolve the duckdb shell (via :func:`find_duckdb`) and return a :class:`DuckShell`.

    `preamble` may be a string, or a callable ``(build_dir) -> str`` for preambles that
    reference locally-built extension paths under the build dir.
    """
    wd = working_dir or getattr(config, "sqllogic_working_dir", None) or os.getcwd()
    shell = find_duckdb(config, wd)
    if callable(preamble):
        preamble = preamble(os.path.dirname(shell))
    return DuckShell(shell, preamble, conflict_markers=conflict_markers, timeout=timeout)
