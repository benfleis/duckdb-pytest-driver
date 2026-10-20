"""Every renderer here reads `04-events.jsonl` and nothing else.

`Report.of(events)` is the one reading of the stream: it merges the runs that were given, keeps the
last `test_end` for a test id, and answers the questions a reader asks (counts, the slowest tests,
what was retried). The renderers turn that into text, JSON or JUnit XML. A fact no
renderer can find here belongs in an event, not in a renderer.

    live       one line per test as it ends, failures in full at the end, then a summary
    text       the same without cursor control, at three levels (terse, normal, verbose)
    json       the event lines as they are, and one document as `05-report.json`/`.yaml`
    junit      JUnit XML for CI annotations: one testsuite per test file
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..events import RUN_END, RUN_START, SERVICE, TEST_END, TEST_START
from ..outcomes import ALL as OUTCOMES
from ..outcomes import BLOCKED, ERROR, FAILED, PASSED

FORMATS = ("text", "json", "junit")
LEVELS = ("terse", "normal", "verbose")
REPORT_YAML, REPORT_JSON = "05-report.yaml", "05-report.json"


@dataclass
class Report:
    runs: list = field(default_factory=list)  # run_start events, in the order the run dirs were given
    ends: dict = field(default_factory=dict)  # run id -> run_end
    tests: list = field(default_factory=list)  # test_end, merged; a later run wins a shared test id
    starts: dict = field(default_factory=dict)  # test id -> test_start
    services: list = field(default_factory=list)
    events: list = field(default_factory=list)  # every line as it was read, for `--format json`

    @classmethod
    def of(cls, events):
        report, by_id = cls(), {}
        report.events = list(events)
        for event in report.events:
            kind = event.get("event")
            if kind == RUN_START:
                report.runs.append(event)
            elif kind == RUN_END:
                report.ends[event.get("run")] = event
            elif kind == SERVICE:
                report.services.append(event)
            elif kind == TEST_START:
                report.starts[event.get("name")] = event
            elif kind == TEST_END:
                by_id[event.get("name")] = event
        report.tests = list(by_id.values())
        return report

    @property
    def merged(self):
        """True when more than one run contributed, so a reader must be told where a test came from."""
        return len({c.get("run") for c in self.tests}) > 1 or len(self.runs) > 1

    @property
    def run_id(self):
        return self.runs[-1].get("run") if self.runs else next((c.get("run") for c in self.tests), None)

    @property
    def run_dir(self):
        return self.runs[-1].get("run_dir") if self.runs else None

    @property
    def counts(self):
        return {o: sum(c.get("outcome") == o for c in self.tests) for o in OUTCOMES}

    @property
    def seconds(self):
        """Wall time: what the runs reported, else the sum of the tests."""
        if self.ends:
            return round(sum(e.get("seconds") or 0 for e in self.ends.values()), 2)
        return round(sum(c.get("seconds") or 0 for c in self.tests), 2)

    @property
    def exit_code(self):
        """What the run returned. Merged runs have no one code, so it is worked out again by the
        rule a run uses: green means it ran."""
        if len(self.runs) == 1 and (end := self.ends.get(self.run_id)) and end.get("exit_code") is not None:
            return end["exit_code"]
        counts = self.counts
        bad = counts[FAILED] + counts[ERROR] + counts[BLOCKED]
        return 0 if bad == 0 and counts[PASSED] > 0 and not self.left_behind else 1

    @property
    def left_behind(self):
        """What the cleanup of each run could not remove, one line each."""
        return [problem for end in self.ends.values() for problem in end.get("cleanup") or ()]

    @property
    def bad(self):
        """The tests a reader must look at, in the order they ended."""
        return [c for c in self.tests if c.get("outcome") in (FAILED, ERROR, BLOCKED)]

    @property
    def not_provisioned(self):
        return {s["name"]: s.get("error", "") for s in self.services if s.get("state") == "failed"}

    def slowest(self, n=5):
        return sorted((c for c in self.tests if c.get("seconds") is not None), key=lambda c: -c["seconds"])[:n]

    def retried(self):
        return [c for c in self.tests if (c.get("attempts") or 1) > 1]

    def by_reason(self, outcome):
        """{reason: [test ids]}, most tests first: one line per reason beats one per test."""
        out = {}
        for test in self.tests:
            if test.get("outcome") == outcome:
                out.setdefault(test.get("reason") or (test.get("problems") or ["no reason given"])[0], []).append(
                    test["name"]
                )
        return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))


def render(report, out, *, format="text", level="normal", color=False):
    from . import document, junit, text

    if format == "text":
        text.render(report, out, level=level, color=color)
    elif format == "json":
        document.render_lines(report, out)
    elif format == "junit":
        junit.render(report, out)
    else:
        raise ValueError(f"format={format!r}; it is one of {FORMATS}")


def write_files(report, *, run_dir=None, report_json=None, junit_file=None):
    """The side outputs, which may be asked for beside any live output. Returns what was written."""
    from . import document, junit

    written = []
    for path in [os.path.join(run_dir, REPORT_YAML)] if run_dir else []:
        document.write_yaml(report, path)
        written.append(path)
    for path in [p for p in (os.path.join(run_dir, REPORT_JSON) if run_dir else None, report_json) if p]:
        document.write_json(report, path)
        written.append(path)
    if junit_file:
        with open(junit_file, "w") as f:
            junit.render(report, f)
        written.append(junit_file)
    return written
