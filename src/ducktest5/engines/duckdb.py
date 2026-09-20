"""The built duckdb shell as a client.

`DuckDBSession` is the rule: one shell process for the whole run, as the unittest binary is one
process for its run. The words are sqllogictest's, where it has them:

    reconnect()   new connection, same instance   the shell has one connection, so it cannot
    restart()     new instance, same process      `.open :memory:`, then the test's preambles again
    respawn()     new process                     not in sqllogictest, which is one process

Each test starts on a fresh instance. `DuckDBPerCall` is the exception: a cold process for every
call.
"""

import json
import os
import queue
import re
import subprocess
import threading

from ..client import SqlError
from ..sqltext import split_statements

_MARK = "<<ducktest:{}>>"


class _Pairs(list):
    """A JSON object kept as its (key, value) pairs."""


def _nested(value):
    if isinstance(value, _Pairs):
        return {k: _nested(v) for k, v in value}
    if isinstance(value, list):
        return [_nested(v) for v in value]
    return value


_ERROR_LINE = re.compile(r"^(?:[A-Za-z]+(?: [A-Za-z]+)* )?Error:", re.M)


def _is_error(output):
    """The shell prints an error as `<Kind> Error: ...`. Text that is neither that nor a JSON result is
    what a statement such as EXPLAIN prints, and is a result."""
    return bool(output) and not output.startswith("[") and bool(_ERROR_LINE.search(output))


def _rows(text):
    """Rows of `duckdb -json` output. A row stays a list of pairs, so two columns of one name
    (`SELECT a.id, b.id`) stay two columns; an object inside a value (a struct, a map) is a dict."""
    if text and not text.startswith("["):
        return ["output"], [(text,)]  # a statement that prints text, not rows: EXPLAIN
    try:
        rows = json.loads(text, object_pairs_hook=_Pairs) if text else []
    except ValueError:
        raise SqlError(f"the duckdb shell printed something that is not a result:\n{text}") from None
    return ([k for k, _ in rows[0]] if rows else []), [tuple(_nested(v) for _, v in row) for row in rows]


class DuckDBSession:
    def __init__(self, binary, *, timeout=120):
        self.binary = binary
        self._timeout = timeout
        self._process = None
        self._lines = None
        self._calls = 0
        self._preambles = []  # of the running test, in order: what `restart` and `respawn` must redo

    # -- lifecycle ------------------------------------------------------------
    def begin_test(self):
        self._preambles = []
        if self._alive():
            try:
                self._dot(".open :memory:")
            except SqlError:
                self.close()  # a shell that cannot reopen is replaced at the next statement

    def add_preamble(self, preamble):
        self._preambles.append(preamble)
        self._run_each(split_statements(preamble), numbered=False)

    def restart(self):
        self._dot(".open :memory:")
        self._replay()

    def respawn(self):
        self.close()
        self._replay()

    def close(self):
        if self._process is not None:
            self._process.kill()
            self._process.wait()
            self._process = None

    def _replay(self):
        for preamble in self._preambles:
            self._run_each(split_statements(preamble), numbered=False)

    def _alive(self):
        return self._process is not None and self._process.poll() is None

    def _spawn(self):
        # stderr joins stdout, so an error arrives in order, before the marker of its statement.
        self._process = subprocess.Popen(
            [self.binary, "-unsigned", "-json", "-init", os.devnull],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._process, self._lines), daemon=True).start()

    @staticmethod
    def _pump(process, lines):
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    # -- statements -----------------------------------------------------------
    def run(self, statements):
        return _rows(self._run_each(statements, numbered=True))

    def _run_each(self, statements, *, numbered):
        """One statement at a time, so the first failure stops the rest, as `-bail` would, and the
        shell lives on. Returns the output of the last statement."""
        if not self._alive():
            self._spawn()
            if numbered:  # the shell died under this test; give it back what the test set up
                self._replay()
        output = ""
        for n, statement in enumerate(statements):
            # The `;` goes on its own line: after a trailing `-- comment` it would be part of the comment,
            # and the shell would then read the marker as more SQL and never answer.
            output = self._send(statement.rstrip().rstrip(";") + "\n;")
            if _is_error(output):
                raise SqlError(output, statement=n if numbered else None)
        return output

    def _dot(self, command):
        if not self._alive():
            self._spawn()
        output = self._send(command)
        if output:
            raise SqlError(f"{command}: {output}")

    def _send(self, text):
        self._calls += 1
        mark = _MARK.format(self._calls)
        self._process.stdin.write(f"{text}\n.print {mark}\n")
        self._process.stdin.flush()
        out = []
        while True:
            try:
                line = self._lines.get(timeout=self._timeout)
            except queue.Empty:
                self.close()
                raise SqlError(f"no answer from the duckdb shell in {self._timeout}s; the shell was killed") from None
            if line is None:
                self._process = None
                raise SqlError("the duckdb shell exited: " + "".join(out).strip())
            if line.strip() == mark:
                return "".join(out).strip()
            out.append(line)


class DuckDBPerCall:
    """A cold process for every call: `preamble`, then the call's statements, with a marker before
    each, so under `-bail` the last marker reached names the statement that failed."""

    def __init__(self, binary, preamble="", *, timeout=120):
        self.binary = binary
        self.preamble = preamble
        self._timeout = timeout

    def run(self, statements):
        script = [self.preamble]
        for n, statement in enumerate(statements):
            script += [f".print {_MARK.format(n)}", statement.rstrip().rstrip(";") + "\n;"]
        p = subprocess.run(
            [self.binary, "-unsigned", "-bail", "-json", "-init", os.devnull],
            input="\n".join(script) + "\n",
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        chunks = p.stdout.split("<<ducktest:")
        reached = len(chunks) - 2  # chunks[0] is the preamble's output
        if p.returncode != 0:
            raise SqlError(p.stderr.strip(), statement=reached if reached >= 0 else None)
        return _rows(chunks[-1].split(">>", 1)[1].strip() if reached >= 0 else "")
