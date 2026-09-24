"""JUnit XML, for the annotations a CI shows beside a pull request.

The container JUnit calls a `testsuite` is the **test file** here: one `<testsuite>` per file, one
`<testcase>` per test, so a CI that groups by suite groups by file. (In ducktest a *suite* is a named
selection of tests; that idea has no place in this file.) Testsuites are sorted by file path and
testcases by id, so the XML does not depend on which test ended first.

    FAILED      <failure type="FAILED">        what is under test went wrong
    ERROR       <error type="ERROR">           something else did, so the test proved nothing
    BLOCKED     <error type="BLOCKED">         a service or engine did not come up; it never ran
    SKIPPED     <skipped message="<reason>">   the binary left it out
    PASSED      nothing                        with the run, the runner and the variants as properties

The text of a `<failure>` or an `<error>` is everything the terminal would show for that test, so a
CI annotation needs no second place to look.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..outcomes import BLOCKED, ERROR, FAILED, SKIPPED
from . import text


def _file_of(test):
    return test.get("file") or str(test.get("id", "")).split("::", 1)[0] or "unknown"


def _properties(test):
    out = {"run": test.get("run"), "runner": test.get("runner"), "type": test.get("type")}
    out.update({f"variant.{axis}": value for axis, value in (test.get("variants") or {}).items()})
    if (test.get("attempts") or 1) > 1:
        out["attempts"] = test["attempts"]
    if test.get("note"):
        out["note"] = test["note"]
    return {k: str(v) for k, v in out.items() if v not in (None, "")}


def build(report):
    # Sorted by id before grouping, so a file's tests stay in that order too: neither depends on
    # which one ended first.
    by_file = {}
    for test in sorted(report.tests, key=lambda c: c.get("id") or ""):
        by_file.setdefault(_file_of(test), []).append(test)
    counts = report.counts
    root = ET.Element(
        "testsuites",
        name="ducktest5",
        tests=str(len(report.tests)),
        failures=str(counts[FAILED]),
        errors=str(counts[ERROR] + counts[BLOCKED]),
        skipped=str(counts[SKIPPED]),
        time=f"{report.seconds}",
    )
    for path, tests in sorted(by_file.items()):
        suite = ET.SubElement(
            root,
            "testsuite",
            name=path,
            tests=str(len(tests)),
            failures=str(sum(c.get("outcome") == FAILED for c in tests)),
            errors=str(sum(c.get("outcome") in (ERROR, BLOCKED) for c in tests)),
            skipped=str(sum(c.get("outcome") == SKIPPED for c in tests)),
            time=f"{round(sum(c.get('seconds') or 0 for c in tests), 2)}",
        )
        for test in tests:
            _testcase(suite, test, path)
    ET.indent(root)
    return root


def _testcase(suite, test, path):
    node = ET.SubElement(
        suite,
        "testcase",
        name=str(test.get("id")),
        classname=path,
        time=f"{test.get('seconds') or 0}",
    )
    if properties := _properties(test):
        holder = ET.SubElement(node, "properties")
        for name, value in properties.items():
            ET.SubElement(holder, "property", name=name, value=value)
    outcome = test.get("outcome")
    if outcome == SKIPPED:
        ET.SubElement(node, "skipped", message=test.get("reason") or "skipped inside the binary")
        return
    if outcome not in (FAILED, ERROR, BLOCKED):
        return
    failure = test.get("failure") or {}
    message = (failure.get("summary") or "\n".join(test.get("problems", ())) or outcome).splitlines()[0][:400]
    element = ET.SubElement(
        node, "failure" if outcome == FAILED else "error", message=message, type=str(outcome).upper()
    )
    element.text = "\n".join(text.failure_block(test, color=False, merged=True))


def render(report, out):
    out.write('<?xml version="1.0" encoding="utf-8"?>\n')
    out.write(ET.tostring(build(report), encoding="unicode"))
    out.write("\n")
