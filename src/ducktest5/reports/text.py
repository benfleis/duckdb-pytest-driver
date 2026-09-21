"""Text reports at three levels, and the pieces the live output is made of.

`normal` is what a terminal shows, without cursor control: one line per case, then every case a
reader must look at in full, then a summary. `verbose` adds, per case, the statements a Python body
ran with their seconds, and the batch a `.test` case ran in. `terse` is the one for an AI reader and
for grep.

TERSE GRAMMAR
-------------
No banner, no passed cases. Zero or more case blocks, then exactly one `summary:` line, which is the
last non-empty line of the output. Blocks are separated by one blank line.

Every line of a block is one of:

    <key>: <value>      a one-line field; a value never holds a newline
    <key>:              a field whose value follows as continuation lines
      | <text>          one line of the value above: two spaces, a bar, one space

Keys are lower case, `[a-z][a-z-]*`, appear at most once in a block, and always in this order:

    case run outcome runner seconds attempts file where reason blocked-by note
    message sql expected actual made-of statements kept again

`case:` is always the first line of a block, so a tool finds blocks without counting blank lines.
`run:` is there only when several runs were merged. `outcome:` is upper case, one of PASSED FAILED
ERROR BLOCKED SKIPPED. The summary line always names all five outcomes, in that order, so a tool can
read it with one pattern:

    summary: 28 passed, 2 failed, 0 error, 0 blocked, 47 skipped in 63.2s; run-dir=<path>

`; from-run-summary=N` follows when N of the cases were decided by the summary of the `run` wrapper
and not by an event of their own, which is a weaker outcome with no seconds.
"""

from __future__ import annotations

from ..outcomes import ALL as OUTCOMES
from ..outcomes import BLOCKED, ERROR, FAILED, PASSED, SKIPPED

# The owner's taste, in one place: the colour of each outcome word (ANSI SGR).
COLOURS = {PASSED: "32", FAILED: "31", ERROR: "35", BLOCKED: "33", SKIPPED: "90"}

# The order of a terse block's lines. A renderer never invents a key that is not here.
TERSE_KEYS = (
    "case",
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
    "expected",
    "actual",
    "made-of",
    "statements",
    "kept",
    "again",
)


def paint(text, code, color):
    return f"\033[{code}m{text}\033[0m" if color and code else text


def case_line(case, *, color=False, merged=False, width=8):
    """One case as it ends: the outcome word, the id, the time."""
    outcome = case.get("outcome", "?")
    seconds = case.get("seconds")
    tail = ""
    if seconds is not None:
        by_client = case.get("seconds_by_client") or {}
        inner = ": " + ", ".join(f"{k} {v}s" for k, v in by_client.items()) if by_client else ""
        tail = f"  ({seconds}s{inner})"
    note = f"  [{case['note']}]" if case.get("note") else ""
    where = f"  <{case['run']}>" if merged and case.get("run") else ""
    return paint(f"{outcome.upper():{width}}", COLOURS.get(outcome), color) + f" {case['id']}{where}{tail}{note}"


def service_line(event):
    """One service or engine, whatever it did."""
    what = f"{event.get('kind', 'service')} {event['id']}"
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
    """What the case was made of, as the `scan --env` lines plus the files that hold the rest."""
    out = []
    for part in (facts or {}).get("environment", ()):
        head = f"{part['what']}: {part['is']}" if part.get("is") else part["what"]
        out.append(f"{head}  <- {part['from']}")
    for name, path in (facts or {}).get("files", {}).items():
        out.append(f"{name}: {path}")
    return out


def statement_lines(case):
    return [
        f"{s.get('seconds')}s  [{s.get('client')}] {_one_line(s.get('sql', ''))}" for s in case.get("statements", ())
    ]


def _one_line(text):
    return " ".join(str(text).split())


def failure_block(case, *, color=False, merged=False, verbose=False):
    """Everything a reader needs about one case that did not pass, with nothing to look up.

    The first problem is shown in its parts where they were found -- the message, the statement, the
    two results -- and verbatim where they were not; the binary's Catch banner and its repeated
    location are already out of the parts. Any further problem follows as it was recorded."""
    out = [paint(f"--- {case['id']}", COLOURS.get(case.get("outcome")), color)]
    if merged and case.get("run"):
        out.append(f"  run: {case['run']}")
    failure = case.get("failure") or {}
    if where := failure.get("where"):
        out.append(f"  {where}")
    if not failure and case.get("note"):
        out += ["", f"  {case['note']}"]
    problems = list(case.get("problems", ()))
    if failure:
        out += ["", *(f"  {line}" for line in str(failure.get("summary", "")).splitlines())]
        for name in ("sql", "expected", "actual"):
            if failure.get(name):
                out.append(f"  {name}:")
                out += [f"    {line}" for line in str(failure[name]).splitlines()]
        problems = problems[1:]
    for problem in problems:
        out += ["", problem]
    if case.get("reason"):
        out += ["", f"  {case['reason']}"]
    out.append("")
    out += [f"  {line}" for line in made_of_lines(case.get("made_of"))]
    if verbose:
        if case.get("batch"):
            out.append(f"  batch: {case['batch']}")
        out += [f"  {line}" for line in statement_lines(case)]
    if case.get("kept"):
        out.append("  kept: " + "\n        ".join(case["kept"]))
    if case.get("again"):
        out.append(f"  again: {case['again']}")
    return out


def summary_lines(report, *, color=False):
    """Counts in a fixed order, the wall time, the slowest cases, what was retried, and where the
    run dir is. A run whose outcomes are not all the binary's own says so first."""
    out, counts = [], report.counts
    if weak := report.weak:
        out.append(
            f"{len(weak)} of {len(report.cases)} cases have no event of their own: the run wrapper forwards none "
            "for a batch that passed, so passed is what its summary says, without seconds"
        )
    for reason, ids in report.by_reason(SKIPPED).items():
        if not out:
            out.append(f"{counts[SKIPPED]} skipped inside the binary:")
        out.append(f"  {len(ids):4}  {reason}")
        out += [f"          {i}" for i in ids[:3]]
        if len(ids) > 3:
            out.append(f"          ... and {len(ids) - 3} more")
    for service in report.services:
        if service.get("state") == "failed":
            blocked = sum(service["id"] in (c.get("blocked_by") or ()) for c in report.cases)
            out += ["", f"{service['id']!r} did not come up: {blocked} cases blocked", f"  {service.get('error', '')}"]
    if slowest := report.slowest(5):
        out += ["", "slowest:"]
        out += [f"  {c['seconds']:>7}s  {c['id']}" for c in slowest]
    if retried := report.retried():
        out += ["", "retried:"]
        for case in retried:
            out.append(f"  {case['id']}  {case.get('note') or 'attempts: ' + str(case.get('attempts'))}")
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


def _message(case, failure):
    """What went wrong, once: a skip says it under `reason` and needs no second line."""
    text = failure.get("summary") or "\n".join(case.get("problems", ())) or None
    return None if text == case.get("reason") else text


def _terse_block(case, *, merged):
    failure = case.get("failure") or {}
    fields = {
        "case": case.get("id"),
        "run": case.get("run") if merged else None,
        "outcome": str(case.get("outcome", "")).upper(),
        "runner": case.get("runner"),
        "seconds": case.get("seconds"),
        "attempts": case.get("attempts"),
        "file": case.get("file"),
        "where": failure.get("where"),
        "reason": case.get("reason"),
        "blocked-by": ", ".join(case.get("blocked_by", ())) or None,
        "note": case.get("note"),
        "message": _message(case, failure),
        "sql": failure.get("sql"),
        "expected": failure.get("expected"),
        "actual": failure.get("actual"),
        "made-of": "\n".join(made_of_lines(case.get("made_of"))) or None,
        "statements": "\n".join(statement_lines(case)) or None,
        "kept": "\n".join(case.get("kept", ())) or None,
        "again": case.get("again"),
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
    for case in report.cases:
        if case.get("outcome") == PASSED:
            continue
        for line in _terse_block(case, merged=merged):
            print(line, file=out)
        print(file=out)
    counts = report.counts
    where = "".join(f"; run-dir={r.get('run_dir')}" for r in report.runs if r.get("run_dir"))
    weak = f"; from-run-summary={len(report.weak)}" if report.weak else ""
    print(
        "summary: " + ", ".join(f"{counts[o]} {o}" for o in OUTCOMES) + f" in {report.seconds}s{where}{weak}",
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
    for case in report.cases:
        print(case_line(case, color=color, merged=merged), file=out)
        if verbose:
            for line in statement_lines(case):
                print(f"         {line}", file=out)
    for case in report.bad:
        print(file=out)
        for line in failure_block(case, color=color, merged=merged, verbose=verbose):
            print(line, file=out)
    print(file=out)
    for line in summary_lines(report, color=color):
        print(line, file=out)
