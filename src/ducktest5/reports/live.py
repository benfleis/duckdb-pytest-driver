"""What a terminal shows while a run goes on.

It is a renderer like the others: it is handed each event as the run writes it, and prints nothing it
did not get from one. So `ducktest5 report --run-dir DIR` of the same run prints the same thing, and
the two can never drift apart.

One line per test as it ends, with a progress count. Tests that did not pass are printed in full at
the end, never in between, so the lines that scroll past stay one per test and a failure is read in
one place. Colour only when stdout is a terminal and `NO_COLOR` is unset.
"""

from __future__ import annotations

import os
import sys

from ..events import RUN_START, SERVICE, TEST_END
from . import text


def wants_colour(out):
    return bool(getattr(out, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


class Live:
    """Feed it every event; call `finish()` when the run is over."""

    def __init__(self, out=None, *, color=None, total=None, level="normal"):
        self.out = out or sys.stdout
        self.color = wants_colour(self.out) if color is None else color
        self.level = level
        self.done = 0
        self._total(total)

    def _total(self, total):
        self.total = total
        self.width = len(f"[{total}/{total}]") if total else 6

    def __call__(self, event):
        kind = event.get("event")
        if kind == RUN_START:
            self._total(event.get("tests") or self.total)
        elif kind == SERVICE:
            self._service(event)
        elif kind == TEST_END:
            self.done += 1
            count = f"[{self.done}/{self.total}]" if self.total else f"[{self.done}]"
            print(f"{count:>{self.width}} {text.test_line(event, color=self.color)}", file=self.out, flush=True)
            if self.level == "verbose":
                for statement in text.statement_lines(event):
                    print(f"{'':>{self.width}}          {statement}", file=self.out, flush=True)

    def _service(self, event):
        print(text.service_line(event), file=self.out, flush=True)

    def finish(self, report):
        """The failures in full, then the summary -- out of the events, as any other renderer."""
        for test in report.bad:
            print(file=self.out)
            for line in text.failure_block(test, color=self.color, verbose=self.level == "verbose"):
                print(line, file=self.out)
        print(file=self.out)
        for line in text.summary_lines(report, color=self.color):
            print(line, file=self.out)
        self.out.flush()
        return report
