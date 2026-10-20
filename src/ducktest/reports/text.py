"""Text reports at three levels, and the pieces the live output is made of.

`normal` is what a terminal shows, without cursor control: one line per test, then every test a
reader must look at in full, then a summary. `verbose` adds, per test, the statements a Python body
ran with their seconds, and the batch a test of a `.test` file ran in. `terse` is the one for an AI reader and
for grep.

TERSE GRAMMAR
-------------
No banner, no passed tests. Zero or more test blocks, then exactly one `summary:` line, which is the
last non-empty line of the output. Blocks are separated by one blank line.

Every line of a block is one of:

    <key>: <value>      a one-line field; a value never holds a newline
    <key>:              a field whose value follows as continuation lines
      | <text>          one line of the value above: two spaces, a bar, one space

Keys are lower case, `[a-z][a-z-]*`, appear at most once in a block, and always in this order:

    test run outcome runner seconds attempts file where reason blocked-by note
    message sql expected actual made-of statements kept again

`test:` is always the first line of a block, so a tool finds blocks without counting blank lines.
`run:` is there only when several runs were merged. `outcome:` is upper case, one of PASSED FAILED
ERROR BLOCKED SKIPPED. The summary line always names all five outcomes, in that order, so a tool can
read it with one pattern:

    summary: 28 passed, 2 failed, 0 error, 0 blocked, 47 skipped in 63.2s; run-dir=<path>

`; from-run-summary=N` follows when N of the tests were decided by the summary of the `run` wrapper
and not by an event of their own, which is a weaker outcome with no seconds.
"""

from __future__ import annotations

from ..outcomes import ALL as OUTCOMES
from ..outcomes import BLOCKED, ERROR, FAILED, PASSED, SKIPPED

# The owner's taste, in one place: the colour of each outcome word (ANSI SGR).
COLOURS = {PASSED: "32", FAILED: "31", ERROR: "35", BLOCKED: "33", SKIPPED: "90"}

# The order of a terse block's lines. A renderer never invents a key that is not here.
TERSE_KEYS = (
    "test",
    "run",
    "outcome",
    "runner",
    "seconds",
    "attempts",
    "file",
    "where",
    "reason",
    "blocked-by",
    "note",
    "message",
    "sql",
    "error",
    "expected",
    "actual",
    "made-of",
    "statements",
    "kept",
    "again",
)


def paint(text, code, color):
    return f"\033[{code}m{text}\033[0m" if color and code else text


def test_line(test, *, color=False, merged=False, width=8):
    """One test as it ends: the outcome word, the id, the time."""
    outcome = test.get("outcome", "?")
    seconds = test.get("seconds")
    tail = ""
    if seconds is not None:
        by_client = test.get("seconds_by_client") or {}
        inner = ": " + ", ".join(f"{k} {v}s" for k, v in by_client.items()) if by_client else ""
        tail = f"  ({seconds}s{inner})"
    note = f"  [{test['note']}]" if test.get("note") else ""
    where = f"  <{test['run']}>" if merged and test.get("run") else ""
    return paint(f"{outcome.upper():{width}}", COLOURS.get(outcome), color) + f" {test['name']}{where}{tail}{note}"


def service_line(event):
    """One service or engine, whatever it did."""
    what = f"{event.get('kind', 'service')} {event['name']}"
    version = f" ({event['version']})" if event.get("version") else ""
    took = f" ({event['seconds']}s)" if event.get("seconds") else ""
    state = event.get("state")
    if state == "starting":
        return f"{what}{version} ..."
    if state == "up":
        return f"{what} -- {event.get('mode') or 'up'}{took}"
    if state == "kept":
        return f"{what} left running; next run: {event.get('attach')}"
    if state == "stop_failed":
        return f"{what} could not be stopped: {event.get('error', '')}"
    return f"{what} -- did not come up: {event.get('error', '')}"


def made_of_lines(facts):
    """What the test was made of, as the `scan --env` lines plus the files that hold the rest."""
    out = []
    for part in (facts or {}).get("environment", ()):
        head = f"{part['what']}: {part['is']}" if part.get("is") else part["what"]
        out.append(f"{head}  <- {part['from']}")
    for name, path in (facts or {}).get("files", {}).items():
        out.append(f"{name}: {path}")
    return out


def statement_lines(test):
    return [
        f"{s.get('seconds')}s  [{s.get('client')}] {_one_line(s.get('sql', ''))}" for s in test.get("statements", ())
    ]


def _one_line(text):
    return " ".join(str(text).split())


def failure_block(test, *, color=False, merged=False, verbose=False):
    """Everything a reader needs about one test that did not pass, with nothing to look up.

    The first problem is shown in its parts where they were found -- the message, the statement, the
    two results -- and verbatim where they were not; the binary's Catch banner and its repeated
    location are already out of the parts. Any further problem follows as it was recorded."""
    out = [paint(f"--- {test['name']}", COLOURS.get(test.get("outcome")), color)]
    if merged and test.get("run"):
        out.append(f"  run: {test['run']}")
    failure = test.get("failure") or {}
    if where := failure.get("where"):
        out.append(f"  {where}")
    if not failure and test.get("note"):
        out += ["", f"  {test['note']}"]
    problems = list(test.get("problems", ()))
    if failure:
        out += ["", *(f"  {line}" for line in str(failure.get("summary", "")).splitlines())]
        for name in ("sql", "error", "expected", "actual"):
            if failure.get(name):
                out.append(f"  {name}:")
                out += [f"    {line}" for line in str(failure[name]).splitlines()]
        problems = problems[1:]
    for problem in problems:
        out += ["", problem]
    if test.get("reason"):
        out += ["", f"  {test['reason']}"]
    out.append("")
    out += [f"  {line}" for line in made_of_lines(test.get("made_of"))]
    if verbose:
        if test.get("batch"):
            out.append(f"  batch: {test['batch']}")
        out += [f"  {line}" for line in statement_lines(test)]
    if test.get("kept"):
        out.append("  kept: " + "\n        ".join(test["kept"]))
    if test.get("again"):
        out.append(f"  again: {test['again']}")
    return out


def summary_lines(report, *, color=False):
    """Counts in a fixed order, the wall time, the slowest tests, what was retried, and where the
    run dir is."""
    out, counts = [], report.counts
    for reason, ids in report.by_reason(SKIPPED).items():
        if not out:
            out.append(f"{counts[SKIPPED]} skipped:")
        out.append(f"  {len(ids):4}  {reason}")
        out += [f"          {i}" for i in ids[:3]]
        if len(ids) > 3:
            out.append(f"          ... and {len(ids) - 3} more")
    for service in report.services:
        if service.get("state") == "failed":
            blocked = sum(service["name"] in (c.get("blocked_by") or ()) for c in report.tests)
            tests = "test" if blocked == 1 else "tests"
            out += [
                "",
                f"{service['name']!r} did not come up: {blocked} {tests} blocked",
                f"  {service.get('error', '')}",
            ]
    if left := report.left_behind:
        out += ["", "cleanup could not remove:"] + [f"  {problem}" for problem in left]
    if slowest := report.slowest(5):
        out += ["", "slowest:"]
        out += [f"  {c['seconds']:>7}s  {c['name']}" for c in slowest]
    if retried := report.retried():
        out += ["", "retried:"]
        for test in retried:
            out.append(f"  {test['name']}  {test.get('note') or 'attempts: ' + str(test.get('attempts'))}")
    counted = ", ".join(paint(f"{counts[o]} {o}", COLOURS.get(o), color) for o in OUTCOMES if counts[o] or o == PASSED)
    out += ["", f"{counted} in {report.seconds}s"]
    for run in report.runs:
        out.append(f"run dir: {run.get('run_dir')}")
    if not report.runs and report.run_dir:
        out.append(f"run dir: {report.run_dir}")
    return out


# -----------------------------------------------------------------------------
# terse
#


def _message(test, failure):
    """What went wrong, once: a skip says it under `reason` and needs no second line."""
    text = failure.get("summary") or "\n".join(test.get("problems", ())) or None
    return None if text == test.get("reason") else text


def _terse_block(test, *, merged):
    failure = test.get("failure") or {}
    fields = {
        "test": test.get("name"),
        "run": test.get("run") if merged else None,
        "outcome": str(test.get("outcome", "")).upper(),
        "runner": test.get("runner"),
        "seconds": test.get("seconds"),
        "attempts": test.get("attempts"),
        "file": test.get("file"),
        "where": failure.get("where"),
        "reason": test.get("reason"),
        "blocked-by": ", ".join(test.get("blocked_by", ())) or None,
        "note": test.get("note"),
        "message": _message(test, failure),
        "sql": failure.get("sql"),
        "error": failure.get("error"),
        "expected": failure.get("expected"),
        "actual": failure.get("actual"),
        "made-of": "\n".join(made_of_lines(test.get("made_of"))) or None,
        "statements": "\n".join(statement_lines(test)) or None,
        "kept": "\n".join(test.get("kept", ())) or None,
        "again": test.get("again"),
    }
    out = []
    for key in TERSE_KEYS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        lines = str(value).splitlines() or [""]
        if len(lines) == 1:
            out.append(f"{key}: {lines[0]}")
        else:
            out.append(f"{key}:")
            out += [f"  | {line}" for line in lines]
    return out


def terse(report, out):
    merged = report.merged
    for test in report.tests:
        if test.get("outcome") == PASSED:
            continue
        for line in _terse_block(test, merged=merged):
            print(line, file=out)
        print(file=out)
    for problem in report.left_behind:
        print(f"cleanup: {problem}", file=out)
    counts = report.counts
    where = "".join(f"; run-dir={r.get('run_dir')}" for r in report.runs if r.get("run_dir"))
    print(
        "summary: " + ", ".join(f"{counts[o]} {o}" for o in OUTCOMES) + f" in {report.seconds}s{where}",
        file=out,
    )


# -----------------------------------------------------------------------------
# normal and verbose
#


def render(report, out, *, level="normal", color=False):
    if level == "terse":
        return terse(report, out)
    verbose = level == "verbose"
    merged = report.merged
    for service in report.services:
        if service.get("state") != "starting":  # live only; an old run's reader has the outcome
            print(service_line(service), file=out)
    for test in report.tests:
        print(test_line(test, color=color, merged=merged), file=out)
        if verbose:
            for line in statement_lines(test):
                print(f"         {line}", file=out)
    for test in report.bad:
        print(file=out)
        for line in failure_block(test, color=color, merged=merged, verbose=verbose):
            print(line, file=out)
    print(file=out)
    for line in summary_lines(report, color=color):
        print(line, file=out)
