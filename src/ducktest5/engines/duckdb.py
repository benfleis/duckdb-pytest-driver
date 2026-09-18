"""The built duckdb shell as a client: one process per call, statements piped in on stdin."""

import json
import subprocess

from ..client import SqlError

_MARK = "<<ducktest:{}>>"

# Spark and Databricks SQL have assert_true built in, so the same self-checking statement runs on all.
_PRELUDE = """
CREATE OR REPLACE TEMP MACRO assert_true(cond) AS
    CASE WHEN cond THEN NULL ELSE error('assert_true failed') END,
  (cond, message) AS
    CASE WHEN cond THEN NULL ELSE error(message) END;
"""


class DuckDBShell:
    def __init__(self, binary, preamble="", *, timeout=120):
        self.binary = binary
        self.preamble = preamble
        self._timeout = timeout

    def run(self, statements):
        """A `.print` marker before each statement says, on stdout, how far the shell got, so under
        `-bail` the last marker names the statement that failed."""
        script = [_PRELUDE, self.preamble]
        for n, statement in enumerate(statements):
            script += [f".print {_MARK.format(n)}", statement.rstrip(";") + ";"]
        p = subprocess.run(
            [self.binary, "-unsigned", "-bail", "-json"],
            input="\n".join(script) + "\n",
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        chunks = p.stdout.split("<<ducktest:")
        reached = len(chunks) - 2  # chunks[0] is the prelude's output
        if p.returncode != 0:
            raise SqlError(p.stderr.strip(), statement=reached if reached >= 0 else None)
        last = chunks[-1].split(">>", 1)[1].strip() if reached >= 0 else ""
        rows = json.loads(last) if last else []
        return (list(rows[0]) if rows else []), [tuple(row.values()) for row in rows]
