"""Diagnostic logging: what the tool itself did, kept apart from the RESULT reporting on stdout
(`reports/`, `--format`, `--level`). Every module logs under `ducktest5` (`logging.getLogger(__name__)`,
a child of it by the package's own name), so one namespace, `NAME`, carries everything here.

Nothing here ever writes to stdout: a terse report has to stay parseable. Diagnostics go to stderr,
at a level `-v`/`-vv` raise, and, once a run has a directory, to `<run dir>/ducktest.log` at DEBUG --
so a run that looked fine on a terse report can still be read back after the fact.
"""

import logging
import os

NAME = "ducktest5"
FILE_NAME = "ducktest.log"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_STDERR_LEVELS = (logging.WARNING, logging.INFO, logging.DEBUG)  # -v count -> level


def configure(verbosity=0):
    """The logger every module logs under, reset for this invocation of the CLI: WARNING on stderr
    by default, INFO at `-v`, DEBUG at `-vv` or more. A process that calls this more than once (the
    self-tests, invoking `main()` many times) starts clean each time, closing what it replaces."""
    logger = logging.getLogger(NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # never onto the root logger: nothing here is another tool's to format
    handler = logging.StreamHandler()
    handler.setLevel(_STDERR_LEVELS[min(verbosity, len(_STDERR_LEVELS) - 1)])
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    return logger


def add_file(path):
    """A second handler at DEBUG, writing to `path` (the run's `ducktest.log`, or `--log-file`)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    handler = logging.FileHandler(path)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logging.getLogger(NAME).addHandler(handler)


def add_run_dir(run_dir):
    add_file(os.path.join(run_dir, FILE_NAME))
