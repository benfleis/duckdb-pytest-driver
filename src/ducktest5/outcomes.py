"""How a test can end.

FAILED: something under test went wrong. ERROR: something else did (setup, teardown, the runner), so
the test proved nothing. BLOCKED: a service or an engine it needs did not come up, so it never ran.
SKIPPED: the unittest binary left it out (a `require` that is not met).
"""

PASSED, FAILED, ERROR, BLOCKED, SKIPPED = "passed", "failed", "error", "blocked", "skipped"
ALL = (PASSED, FAILED, ERROR, BLOCKED, SKIPPED)
