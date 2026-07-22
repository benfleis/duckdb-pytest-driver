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

import json
import os
import subprocess

from .plugin import find_duckdb


class DuckShell:
    """Stateless SQL runner bound to a resolved duckdb shell + a fixed preamble."""

    def __init__(self, shell, preamble, *, conflict_markers=(), timeout=60):
        self.shell = shell
        self.build_dir = os.path.dirname(shell)
        self.preamble = preamble
        self._conflict_markers = tuple(m.lower() for m in conflict_markers)
        self._timeout = timeout

    # -- reads ----------------------------------------------------------------
    def query(self, sql):
        """Run `sql`; return rows as a list of dicts. Raises on error."""
        p = self._run(sql, json_out=True)
        if p.returncode != 0:
            raise RuntimeError(f"query failed: {p.stderr.strip()}\n  {sql}")
        return last_json_array(p.stdout)

    def column(self, sql, key=None):
        """First column (or `key`) of `sql` as a list."""
        rows = self.query(sql)
        if not rows:
            return []
        key = key or next(iter(rows[0]))
        return [r[key] for r in rows]

    def scalar(self, sql):
        """Single top-left value of `sql` (or None if empty)."""
        col = self.column(sql)
        return col[0] if col else None

    # -- writes ---------------------------------------------------------------
    def exec(self, sql):
        """Run a statement for effect (DDL/DML). Raises on any error."""
        p = self._run(sql)
        if p.returncode != 0:
            raise RuntimeError(f"exec failed: {p.stderr.strip()}\n  {sql}")

    def commit(self, sql):
        """Run a writing statement. With `conflict_markers` set: returns True on commit,
        False on a retryable conflict; raises on any other error. Without markers: raises
        on any error (there is no conflict concept to report)."""
        p = self._run(sql)
        if p.returncode == 0:
            return True
        if self._conflict_markers and any(m in p.stderr.lower() for m in self._conflict_markers):
            return False
        raise RuntimeError(f"commit failed: {p.stderr.strip()}\n  {sql}")

    # -- internal -------------------------------------------------------------
    def _run(self, sql, *, json_out=False):
        cmd = [self.shell, "-unsigned"] + (["-json"] if json_out else []) + ["-c", self.preamble + sql]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout)


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


def last_json_array(stdout):
    """The LAST top-level JSON array in `stdout`.

    `duckdb -json` prints one array per result-returning statement, so preamble statements
    (e.g. a `CREATE SECRET` that returns ``[{"Success":true}]``) precede the query's array.
    Decode the concatenated stream and keep the final array.
    """
    dec = json.JSONDecoder()
    s = stdout.strip()
    idx, last = 0, []
    while idx < len(s):
        while idx < len(s) and s[idx].isspace():
            idx += 1
        if idx >= len(s):
            break
        last, idx = dec.raw_decode(s, idx)
    return last or []
