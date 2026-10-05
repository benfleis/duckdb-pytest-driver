"""`04-events.jsonl`: what a run did, and the only thing a report reads.

One JSON object per line, written as things happen. The live output of a run is a renderer fed the
same objects as they are written, so what a terminal shows and what `ducktest5 report` shows of an
old run come from one place and cannot drift apart. Nothing is ever recomputed from the test tree.

Two rules for a reader: a line whose `event` it does not know is ignored, and a field that is not
there was not known -- never read an absent field as an empty one. Every line has `event` and `run`
(the run id, which is also the run directory's name). Times are seconds, rounded to 2 decimals.

A passed test is a short line. Everything a reader needs to understand a failure is on the failed
test's own line, so no renderer has to open another file.

    run_start     at             when the run started, UTC, ISO-8601
                  command        the command line as given, credential-looking values masked
                  run_dir        where the run writes
                  project_root   the tree that was scanned
                  working_dir    where the command ran; every test path is relative to it
                  selection      what was asked for: paths, select, skip, ids, suites
                  versions       ducktest5, python, duckdb
                  binaries       the duckdb shell and the unittest binary of this run
                  tests          how many tests were selected

    service       id             the service or the engine
                  kind           "service" or "engine"
                  state          "up" or "failed"
                  mode           how a service was provisioned (managed, existing, kept, found running)
                  seconds        how long it took to come up
                  address        where a service answers
                  error          why it did not come up
                  log            the engine's log file

    test_start    id             the test id, `path::name[params,axis=value]`; written when the batch
                                 of the test starts, so what has no `test_end` is what was under way
                  test           `path::name`, the test definition it comes from
                  type           sqllogic | paired | python
                  file           the test's file, relative to the working directory
                  runner         unittest | python
                  at             seconds since run_start

    test_end      id, test, type, file, runner    the same identity as test_start
                  params         the row of the definition's `params=` table, or null
                  variants       axis -> the id of the chosen value
                  tags           the test's tags
                  outcome        passed | failed | error | blocked | skipped
                  seconds        the whole test, retries included
                  seconds_by_client   client name -> seconds spent in it (Python bodies)
                  attempts       how often it ran; absent means once
                  note           one line for the reader: "passed on attempt 2 of 3"
                  problems       the failures, verbatim, in the order they happened
                  failure        the first problem laid out: where (file:line), summary, sql,
                                 expected, actual. A renderer that has no use for the parts
                                 prints `problems`; both say the same thing.
                  reason         why a SKIPPED test was skipped
                  blocked_by     the services or engines that did not come up
                  made_of        the test environment and the files that hold the rest
                  statements     what a Python body ran: client, sql, seconds
                  batch          the batch directory of a test of a `.test` file, relative to the run dir
                  dirs           what the test wrote under the run dir, for the cleanup phase
                  evidence       how the outcome is known: `event` (the binary said so) or
                                 `run-summary` (a runner that forwards no event; weaker, no seconds)
                  kept           paths and URIs the run left behind for a second look
                  again          the command that runs this one test again

    run_end       stopped        the signal that ended the run early, where one did
                  counts         outcome -> how many tests
                  tests          how many tests ended
                  seconds        wall time of the whole run
                  exit_code      what the command returned
                  at             when the run ended, UTC, ISO-8601

Several event files merge: a later file's `test_end` wins for a test id it shares with an earlier
one, and every test keeps the `run` it came from.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import threading

from .provision import MASK

EVENTS_FILE = "04-events.jsonl"
RUN_START, RUN_END, TEST_START, TEST_END, SERVICE = "run_start", "run_end", "test_start", "test_end", "service"

# A command-line token whose name says its value is a credential.
_SECRET_TOKEN = re.compile(r"^(--?[\w-]*(?:secret|token|password|key)[\w-]*=).*$", re.IGNORECASE)

# "file.test:12", as both the binary and a Python traceback name a place.
_LOCATION = re.compile(r"(?<![\w./+-])([\w./+-]+\.(?:test|test_slow|test_coverage|sqllogic|py)):(\d+)\b")
_RULE = re.compile(r"^=+\s*$")
_CATCH_TAIL = re.compile(r"^(?:~{10,}|unittest is a Catch )")
_NOISE = re.compile(r"^(?:Filters:|\d+\. [\w./+-]+:\d+$|[\w./+-]+:\d+$)")
_MARKERS = {"expected result:": "expected", "actual result:": "actual"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_command(argv):
    """The command line for the record. A value given as `--flag=VALUE` whose flag name says
    credential is masked; a credential passed as a separate word cannot be told from a path, so
    nothing tries."""
    return [_SECRET_TOKEN.sub(r"\1" + MASK, str(a)) for a in argv]


def duckdb_version(binary):
    if not binary:
        return None
    try:
        return subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or None
    except Exception:
        return None


def where_in(text):
    """The first `file:line` in `text`, as the binary and a traceback write it."""
    m = _LOCATION.search(text or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None


def binary_failure(text):
    """What the unittest binary printed, laid out: summary, sql, error, expected, actual, where.

    The binary frames the parts of a failure with lines of `=` and names two of them ("Expected
    result:", "Actual result:"), then Catch2 appends a banner that says nothing about the test.
    The first framed part is the summary (with any text before the first rule, such as where an
    init statement came from), the second the statement; every later one (DuckDB's error, how the
    rows differ) is `error`, so nothing is lost. Text with no rules keeps its first two parts."""
    if not (text or "").strip():
        return {}
    segments, current, bucket, buckets, lead = [], [], None, {}, None

    def flush():
        nonlocal current
        if current:
            (buckets.setdefault(bucket, []) if bucket else segments).append("\n".join(current))
        current = []

    for line in text.splitlines():
        if _CATCH_TAIL.match(line):
            break
        if marker := _MARKERS.get(line.strip().lower()):
            flush()
            bucket = marker  # everything after the marker belongs to it, rules in between and all
        elif _RULE.match(line):
            flush()
            if lead is None:
                lead, segments = segments, []
        else:
            current.append(line)
    flush()

    def meaningful(segment):
        return "\n".join(line for line in segment.splitlines() if line.strip() and not _NOISE.match(line.strip()))

    parts = [p for p in map(meaningful, segments) if p]
    if lead is not None:
        first = "\n".join(p for p in [*map(meaningful, lead), parts[0] if parts else ""] if p)
        rest = [s.strip("\n") for s in segments if meaningful(s)][2:]  # as printed
        parts = [first, *parts[1:2]] if first else parts[:2]
    else:
        rest = []
    out = {"summary": parts[0]} if parts else {}
    if len(parts) > 1:
        out["sql"] = parts[1]
    if rest:
        out["error"] = "\n\n".join(rest)
    for name, parts in buckets.items():
        if joined := "\n".join(parts).strip("\n"):
            out[name] = joined
    if where := where_in(text):
        out["where"] = where
    return out


class Writer:
    """Writes the event file, and hands every event to the renderers that watch the run live.

    A renderer sees exactly what the file holds, so a live run and `ducktest5 report` of it later
    cannot disagree. Every line is flushed: a run that is killed still explains itself."""

    def __init__(self, path, run_id, *, listeners=(), append=False):
        self.run_id = run_id
        self.path = path
        self.events = []  # what was written, so the run can report without reading its own file back
        self._file = open(path, "a" if append else "w")
        self._listeners = list(listeners)
        self._lock = threading.Lock()  # the batches write from their own threads

    def emit(self, event, **fields):
        line = {"event": event, "run": self.run_id, **{k: v for k, v in fields.items() if v not in (None, (), {})}}
        with self._lock:
            self.events.append(line)
            self._file.write(json.dumps(line, default=str) + "\n")
            self._file.flush()
            for listener in self._listeners:
                listener(line)
        return line

    def close(self):
        self._file.close()


def files_in(path):
    """The event files of a run directory, oldest name first: `04-events.jsonl` and any slice beside
    it, which is what two machines that share one plan write (`exec --events FILE`)."""
    if not os.path.isdir(path):
        return [path]
    stem, suffix = EVENTS_FILE.split(".", 1)
    names = [n for n in os.listdir(path) if n.startswith(stem) and n.endswith("." + suffix)]
    return [os.path.join(path, n) for n in sorted(names, key=lambda n: (n != EVENTS_FILE, n))]


def read(path):
    """The events of one file, or of one run directory. A half-written last line is dropped: a run
    that was killed mid-line is still readable."""
    out = []
    for name in files_in(path):
        if not os.path.isfile(name):
            continue
        with open(name) as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    out.append(event)
    return out


def load(paths):
    """The events of several run directories, in the order given."""
    out = []
    for path in paths:
        out += read(path)
    return out


def versions(duckdb_bin=None):
    """What a reader needs to tell two runs apart. The driver's own version comes from the installed
    distribution, which is absent when it runs from a checkout."""
    try:
        from importlib.metadata import version

        mine = version("duckdb-pytest-driver")
    except Exception:
        mine = None
    return {"ducktest5": mine, "python": sys.version.split()[0], "duckdb": duckdb_version(duckdb_bin)}
