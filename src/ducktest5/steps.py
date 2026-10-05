"""`ctx.step(message)`: a coarse operation of a body -- start a server, seed a table -- logged with
its time, at INFO: on stderr with `-v`, and always in `<run dir>/ducktest.log`.

    with ctx.step("seeding the table"):
        ...

Pass `logger=` to log under an extension's own namespace.
"""

import logging
import time
from contextlib import contextmanager

_log = logging.getLogger("ducktest5.step")


@contextmanager
def step(message, *, logger=_log):
    """Log + time a high-level op at INFO. See module docstring."""
    logger.info(message)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        logger.info(f"{message} -- done ({time.perf_counter() - t0:.1f}s)")
