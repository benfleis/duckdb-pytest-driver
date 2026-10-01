"""How a test can end.

FAILED: something under test went wrong. ERROR: something else did (setup, teardown, the runner), so
the test proved nothing. BLOCKED: a service or an engine it needs did not come up, so it never ran.
SKIPPED: a condition of the test itself is not met: a `require` in the unittest binary, or a Python
body that called `skip(reason)`.
"""

from .binary import UsageError

PASSED, FAILED, ERROR, BLOCKED, SKIPPED = "passed", "failed", "error", "blocked", "skipped"
ALL = (PASSED, FAILED, ERROR, BLOCKED, SKIPPED)


class Skip(BaseException):
    """Ends a Python body as SKIPPED. A BaseException, like KeyboardInterrupt, so that a body's own
    `except Exception:` does not swallow it."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def skip(reason):
    """End the running Python body as SKIPPED, with `reason` in the report. For a condition the body
    checks itself (a binary not built, a feature off in this build); an input that does not come up is
    declared and makes the test BLOCKED instead. Works from any helper the body calls."""
    if not isinstance(reason, str) or not reason.strip():
        raise UsageError(f"skip({reason!r}): give the reason, as text a report can show")
    raise Skip(reason)
