"""The workers of the exec phase: what runs at the same time, and what each one owns.

Threads, not processes, for Python bodies. Probed here with pyspark 4.2 and delta-spark 4.4: one
Spark session boots in 13.3 s, so four processes would boot four JVMs and hold four copies of what a
JVM holds. Four threads on one session ran their SQL at the same time instead -- four bodies whose
own times add up to 11.0 s took 2.9 s of wall time, against 2.0 s for one of them alone.

What a worker owns, and never shares, are its Sessions (`resources/session.py`), one for each
identity its tests need, reset between tests and reopened after a failed one:

- Its own DuckDB shell. A shell takes one statement at a time behind a lock, so two workers on one
  shell would wait for each other; no shell is ever shared between workers. It is the machinery a
  client of its own already uses, one warm shell for each worker and not one for each test. A test
  whose environment sets or unsets process variables has a Session of another identity, so a shell
  of its own: the environment of a thread cannot differ from the process's.
- Its own Spark session, a clone of the run's. Temp views are session state, so the drop of them
  before a test would else reach into the test running beside it. A brand new session isolates the
  views too, but starts with an empty configuration -- the `fs.s3a.*` settings the engine was given
  for a service were gone. A clone inherits them, and shares the JVM, the context and the cache:
  still one boot for every test that needs the engine.

On a suite of 43 tests that drive Spark and DuckDB in turn, this is 400 s of wall time at one
worker, 271 s at two and 196 s at four, for the same outcome of every test. Four workers are not
four times as fast: the engine is one local Spark with two task slots, and that is what they queue
for.

`.test` batches and Python tests share one budget of `--workers` slots -- a batch or a test takes
one, whichever is ready for it first -- and take them in plan order. Two tests that name the same
mutex never overlap: a test takes all its named locks, in sorted order, so no two can take theirs in
an order that would deadlock.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import threading
import time

from .resources import RW
from .resources.made import Made
from .resources.session import WORKER

JOIN_GRACE_S = 2.0  # how long join() waits for a thread a timeout already gave up on


class Pool:
    """The `--workers` slots, the Sessions of each worker, and the mutexes."""

    def __init__(self, workers, *, opener, made=None):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ducktest")
        self._opener = opener  # SessionDef -> Opener
        self._made = made or Made()
        self._mine = threading.local()
        self._sessions = []  # every Session made, so that the run can close them all
        self._mutexes = {}
        self._running = {}  # test id -> {session id: Session}, while it runs
        self._guard = threading.Lock()

    def submit(self, fn, *args):
        return self._pool.submit(fn, *args)

    def take(self, test_id, definitions):
        """The test's Sessions, acquired: a worker-scoped one is this worker's instance of that
        identity, made when it is first needed and warm after; any other is made for this test.
        Registered under `test_id` until `give_back`, so a timeout noticed on another thread can
        reach them (`cancel`)."""
        mine = getattr(self._mine, "sessions", None)
        if mine is None:
            mine = self._mine.sessions = {}
        taken = {}
        try:
            for d in definitions:
                session = d.make(self._made.inputs_of(d), run_token=self._made.run_token, opener=self._opener(d))
                if d.scope == WORKER:
                    session = mine.setdefault(session.identity, session)
                with self._guard:
                    if session not in self._sessions:
                        self._sessions.append(session)
                session.acquire(RW)
                taken[d.id] = session
        except BaseException:
            self.give_back(test_id, taken, failed=True)
            raise
        with self._guard:
            self._running[test_id] = taken
        return taken

    def give_back(self, test_id, sessions, *, failed):
        with self._guard:
            self._running.pop(test_id, None)
        for session in sessions.values():
            session.release(session, RW, failed=failed)
            if session.definition.scope != WORKER:
                session.close()

    def cancel(self, test_id):
        """A test's timeout expired: cancel its jobs where the engine can, and close its Sessions,
        so a statement stuck in one releases as an exception and the next test opens them again.
        The worker's thread is not touched -- Python cannot kill it -- and keeps running whatever
        it was doing until that releases it too."""
        with self._guard:
            sessions = self._running.get(test_id, {})
        for session in sessions.values():
            live = session.live
            try:
                if hasattr(live, "cancel_jobs"):
                    live.cancel_jobs(test_id)
                session.close()
            except Exception:
                pass

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
        """Nothing that is still waiting starts, and every Session this pool made is closed --
        which is what ends the statement a body of a stopped run is waiting for."""
        self._pool.shutdown(wait=False, cancel_futures=True)
        for session in list(self._sessions):
            try:
                session.close()
            except Exception:
                pass

    def join(self, timeout=JOIN_GRACE_S):
        """What still runs, run out, for up to `timeout`. It is called after everything a body can
        wait for is already gone -- what is left past `timeout` is a thread a test's own timeout gave
        up on; it cannot be killed, so it is left to finish on its own, unobserved, and the run ends
        without it."""
        deadline = time.monotonic() + timeout
        for thread in list(self._pool._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        self._pool.shutdown(wait=False)
