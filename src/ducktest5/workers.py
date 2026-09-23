"""The workers of the exec phase: what runs at the same time, and what each one owns.

Threads, not processes, for Python bodies. Probed here with pyspark 4.2 and delta-spark 4.4: one
Spark session boots in 13.3 s, so four processes would boot four JVMs and hold four copies of what a
JVM holds. Four threads on one session ran their SQL at the same time instead -- four bodies whose
own times add up to 11.0 s took 2.9 s of wall time, against 2.0 s for one of them alone.

What a worker owns, and never shares:

- Its own DuckDB shell. A shell takes one statement at a time behind a lock, so two workers on one
  shell would wait for each other; no shell is ever shared between workers. It is the machinery a
  client of its own already uses, one warm shell for each worker and not one for each case. A case
  whose environment sets or unsets process variables still gets a shell of its own, as it did when
  cases ran one after another: the environment of a thread cannot differ from the process's.
- Its own Spark session, a clone of the run's. Temp views are session state, so the drop of them
  before a test would else reach into the case running beside it. A brand new session isolates the
  views too, but starts with an empty configuration -- the `fs.s3a.*` settings the engine was given
  for a service were gone. A clone inherits them, and shares the JVM, the context and the cache:
  still one boot for every case that needs the engine.

`.test` batches and Python cases share one budget of `--workers` slots -- a batch or a case takes
one, whichever is ready for it first -- and take them in plan order. Two cases that name the same
mutex never overlap: a case takes all its named locks, in sorted order, so no two can take theirs in
an order that would deadlock.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import threading


class Pool:
    """The `--workers` slots, the DuckDB shell and engine handles of each worker, and the mutexes."""

    def __init__(self, workers, *, make_session, handles=None):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ducktest")
        self._make_session = make_session
        self._handles = handles if handles is not None else {}
        self._mine = threading.local()
        self._sessions = []  # every shell handed out, so that the run can close them all
        self._mutexes = {}
        self._guard = threading.Lock()

    def submit(self, fn, *args):
        return self._pool.submit(fn, *args)

    def session(self):
        """This worker's DuckDB shell: made when it first runs a case, warm for every case after."""
        shell = getattr(self._mine, "session", None)
        if shell is None:
            shell = self._mine.session = self._make_session()
            with self._guard:
                self._sessions.append(shell)
        return shell

    def engines(self):
        """This worker's engine handles. An engine that can give a worker one of its own is asked
        for it; the rest are the run's handle itself. A worker's handle is never stopped: the engine
        it belongs to is the run's, and the run stops that one."""
        handles = getattr(self._mine, "engines", None)
        if handles is None:
            handles = self._mine.engines = {
                i: h.for_worker() if hasattr(h, "for_worker") else h for i, h in self._handles.items()
            }
        return handles

    @contextlib.contextmanager
    def holding(self, names):
        """The named locks, taken in sorted order and released in the other one."""
        taken = []
        try:
            for name in sorted(set(names)):
                with self._guard:
                    lock = self._mutexes.setdefault(name, threading.Lock())
                lock.acquire()
                taken.append(lock)
            yield
        finally:
            for lock in reversed(taken):
                lock.release()

    def stop(self):
        """Nothing that is still waiting starts, and every shell this pool made is closed -- which
        is what ends the statement a body of a stopped run is waiting for."""
        self._pool.shutdown(wait=False, cancel_futures=True)
        for shell in list(self._sessions):
            try:
                shell.close()
            except Exception:
                pass

    def join(self):
        """What still runs, run out. It is called after everything a body can wait for is gone."""
        self._pool.shutdown(wait=True)
