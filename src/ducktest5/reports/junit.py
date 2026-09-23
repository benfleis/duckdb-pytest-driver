"""JUnit XML, for the annotations a CI shows beside a pull request.

The container JUnit calls a `testsuite` is the **test file** here: one `<testsuite>` per file, one
`<testcase>` per case, so a CI that groups by suite groups by file. (In ducktest a *suite* is a named
selection of cases; that idea has no place in this file.) Testsuites are sorted by file path and
testcases by id, so the XML does not depend on which case ended first.

    FAILED      <failure type="FAILED">        what is under test went wrong
    ERROR       <error type="ERROR">           something else did, so the test proved nothing
    BLOCKED     <error type="BLOCKED">         a service or engine did not come up; it never ran
    SKIPPED     <skipped message="<reason>">   the binary left it out
    PASSED      nothing                        with the run, the runner and the variants as properties

The text of a `<failure>` or an `<error>` is everything the terminal would show for that case, so a
CI annotation needs no second place to look.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..outcomes import BLOCKED, ERROR, FAILED, SKIPPED
from . import text


def _file_of(case):
    return case.get("file") or str(case.get("id", "")).split("::", 1)[0] or "unknown"


def _properties(case):
    out = {"run": case.get("run"), "runner": case.get("runner"), "type": case.get("type")}
    out.update({f"variant.{axis}": value for axis, value in (case.get("variants") or {}).items()})
    if (case.get("attempts") or 1) > 1:
        out["attempts"] = case["attempts"]
    if case.get("note"):
        out["note"] = case["note"]
    return {k: str(v) for k, v in out.items() if v not in (None, "")}


def build(report):
    # Sorted by id before grouping, so a file's cases stay in that order too: neither depends on
    # which one ended first.
    by_file = {}
    for case in sorted(report.cases, key=lambda c: c.get("id") or ""):
        by_file.setdefault(_file_of(case), []).append(case)
    counts = report.counts
    root = ET.Element(
        "testsuites",
        name="ducktest5",
        tests=str(len(report.cases)),
        failures=str(counts[FAILED]),
        errors=str(counts[ERROR] + counts[BLOCKED]),
        skipped=str(counts[SKIPPED]),
        time=f"{report.seconds}",
    )
    for path, cases in sorted(by_file.items()):
        suite = ET.SubElement(
            root,
            "testsuite",
            name=path,
            tests=str(len(cases)),
            failures=str(sum(c.get("outcome") == FAILED for c in cases)),
            errors=str(sum(c.get("outcome") in (ERROR, BLOCKED) for c in cases)),
            skipped=str(sum(c.get("outcome") == SKIPPED for c in cases)),
            time=f"{round(sum(c.get('seconds') or 0 for c in cases), 2)}",
        )
        for case in cases:
            _testcase(suite, case, path)
    ET.indent(root)
    return root


def _testcase(suite, case, path):
    node = ET.SubElement(
        suite,
        "testcase",
        name=str(case.get("id")),
        classname=path,
        time=f"{case.get('seconds') or 0}",
    )
    if properties := _properties(case):
        holder = ET.SubElement(node, "properties")
        for name, value in properties.items():
            ET.SubElement(holder, "property", name=name, value=value)
    outcome = case.get("outcome")
    if outcome == SKIPPED:
        ET.SubElement(node, "skipped", message=case.get("reason") or "skipped inside the binary")
        return
    if outcome not in (FAILED, ERROR, BLOCKED):
        return
    failure = case.get("failure") or {}
    message = (failure.get("summary") or "\n".join(case.get("problems", ())) or outcome).splitlines()[0][:400]
    element = ET.SubElement(
        node, "failure" if outcome == FAILED else "error", message=message, type=str(outcome).upper()
    )
    element.text = "\n".join(text.failure_block(case, color=False, merged=True))


def render(report, out):
    out.write('<?xml version="1.0" encoding="utf-8"?>\n')
    out.write(ET.tostring(build(report), encoding="unicode"))
    out.write("\n")
