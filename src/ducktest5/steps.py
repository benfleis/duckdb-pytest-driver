"""High-level operation logging for drivers/fixtures.

`step()` wraps a coarse operation -- start a service, seed a table, clean a temp dir --
so it shows up with timing. It logs at INFO via `logging`: visible live with
`pytest --log-cli-level=INFO`, and in the per-phase "Captured log" section on failure.

    from ducktest import step

    with step("starting OSS UC docker image"):
        ...

NOTE: plain `-v` does NOT surface these. pytest captures stdout/terminal during a test,
so plugin writes are swallowed (shown only on failure); the logging plugin's live-log,
enabled by --log-cli-level, is the reliable live channel. Pass `logger=` to log under an
extension's own namespace (default: the "driver" logger).
"""

import logging
import time
from contextlib import contextmanager

_log = logging.getLogger("driver")


@contextmanager
def step(message, *, logger=_log):
    """Log + time a high-level op at INFO. See module docstring."""
    logger.info(message)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        logger.info(f"{message} -- done ({time.perf_counter() - t0:.1f}s)")
