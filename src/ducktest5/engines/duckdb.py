"""The built duckdb shell as a client.

`DuckDBShell` is one shell process for each engine instance, as the unittest binary is one process
for its batch. Each test starts on a fresh instance in it (`.open :memory:`); a shell that died
under a test is started again, with the test's preambles.

It runs with a private HOME, as the unittest binary does: the persistent secrets and the installed
extensions of whoever starts the run stay out of the tests, and what a test persists stays out of
their home.
"""

import json
import logging
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from ..resources.client import SqlError
from ..resources.credential import engine_env
from ..resources.engine import Engine, EngineDef, environ
from ..sqltext import split_statements

_log = logging.getLogger(__name__)

_MARK = "<<ducktest:{}>>"


class _Pairs(list):
    """A JSON object kept as its (key, value) pairs."""


def _nested(value):
    if isinstance(value, _Pairs):
        return {k: _nested(v) for k, v in value}
    if isinstance(value, list):
        return [_nested(v) for v in value]
    return value


class _Home:
    """The HOME of a client's shell processes: the directory given, else a temporary one that goes
    away with the client."""

    def __init__(self, path=None, env=None):
        self._temporary = None if path else tempfile.TemporaryDirectory(prefix="ducktest-home-")
        self.path = os.path.abspath(path or self._temporary.name)
        self._env = env  # the test environment's, where it has one; else what any engine starts from

    def env(self):
        os.makedirs(self.path, exist_ok=True)  # DuckDB fails on a home directory that is set and absent
        return {**(engine_env() if self._env is None else self._env), "HOME": self.path}


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


DEFAULT_TIMEOUT_S = 120  # for one statement


@dataclass(eq=False)
class DuckDBEngineDef(EngineDef):
    """DuckDB as an engine: a shell process of its own for each instance, with the process
    variables of `process_env`."""

    kind = "duckdb"
    dialect = "duckdb"

    def open(self, runtime):
        return runtime.shell(environ(self.process_env))


class DuckDBEngine(Engine):
    def run(self, statements, *, timeout=None):
        return self.live.run(statements, timeout=timeout or self.definition.timeout or DEFAULT_TIMEOUT_S)


DuckDBEngineDef.instance = DuckDBEngine


def duckdb_engine(id="duckdb", **fields) -> DuckDBEngineDef:
    return DuckDBEngineDef(id, **fields)


class DuckDBShell:
    def __init__(self, binary, *, timeout=DEFAULT_TIMEOUT_S, home=None, env=None):
        self.binary = binary
        self._timeout = timeout
        # One call at a time: two threads would else start two shells, or read each other's output.
        self._busy = threading.RLock()
        self._home = _Home(home, env)
        self._process = None
        self._lines = None
        self._calls = 0
        self._preambles = []  # of the running test, in order: what a new shell must redo

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
        args = [self.binary, "-unsigned", "-json", "-init", os.devnull]
        _log.debug("duckdb shell: %s", shlex.join(args))
        self._process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=self._home.env(),
        )
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._process, self._lines), daemon=True).start()

    @staticmethod
    def _pump(process, lines):
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    # -- statements -----------------------------------------------------------
    def run(self, statements, *, timeout=None):
        """`timeout` is for each statement, in seconds; a statement that is not done by then gets the
        shell killed, also when it still prints."""
        return _rows(self._run_each(statements, numbered=True, timeout=timeout))

    def _run_each(self, statements, *, numbered, timeout=None):
        """One statement at a time, so the first failure stops the rest, as `-bail` would, and the
        shell lives on. Returns the output of the last statement."""
        with self._busy:
            if not self._alive():
                self._spawn()
                if numbered:  # the shell died under this test; give it back what the test set up
                    self._replay()
            output = ""
            for n, statement in enumerate(statements):
                # The `;` goes on its own line: after a trailing `-- comment` it would be part of the
                # comment, and the shell would then read the marker as more SQL and never answer.
                output = self._send(statement.rstrip().rstrip(";") + "\n;", timeout)
                if _is_error(output):
                    raise SqlError(output, statement=n if numbered else None)
            return output

    def _dot(self, command):
        with self._busy:
            if not self._alive():
                self._spawn()
            output = self._send(command)
        if output:
            raise SqlError(f"{command}: {output}")

    def _send(self, text, timeout=None):
        limit = timeout or self._timeout
        with self._busy:
            self._calls += 1
            mark = _MARK.format(self._calls)
            self._process.stdin.write(f"{text}\n.print {mark}\n")
            self._process.stdin.flush()
            out, deadline = [], time.monotonic() + limit
            while True:
                try:
                    line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    self.close()
                    raise SqlError(f"the statement was not done in {limit}s; the duckdb shell was killed") from None
                if line is None:
                    self._process = None
                    raise SqlError("the duckdb shell exited: " + "".join(out).strip())
                if line.strip() == mark:
                    return "".join(out).strip()
                out.append(line)
