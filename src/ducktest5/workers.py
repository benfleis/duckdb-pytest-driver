"""The workers of the exec phase: what runs at the same time, and what each one owns.

Threads, not processes, for Python bodies. Probed here with pyspark 4.2 and delta-spark 4.4: one
Spark session boots in 13.3 s, so four processes would boot four JVMs and hold four copies of what a
JVM holds. Four threads on one session ran their SQL at the same time instead -- four bodies whose
own times add up to 11.0 s took 2.9 s of wall time, against 2.0 s for one of them alone.

What a worker owns, and never shares, are its engine instances (`resources/engine.py`) of lifetime
`worker`, one for each identity its tests need, reset between tests and reopened after a failed one:

- Its own DuckDB shell. A shell takes one statement at a time behind a lock, so two workers on one
  shell would wait for each other; no shell is ever shared between workers. It is the machinery a
  client of its own already uses, one warm shell for each worker and not one for each test. A test
  whose environment sets or unsets process variables has an engine of another identity, so a shell
  of its own: the environment of a thread cannot differ from the process's.
- Its own Spark session, a clone of the run's. Temp views are session state, so the drop of them
  before a test would else reach into the test running beside it. A brand new session isolates the
  views too, but starts with an empty configuration -- what the engine was booted with was gone.
  A clone inherits it, and shares the JVM, the context and the cache:
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
from .resources.engine import BATCH, RUN, WORKER
from .resources.made import Made

JOIN_GRACE_S = 2.0  # how long join() waits for a thread a timeout already gave up on


class Pool:
    """The `--workers` slots, the engine instances of each worker and of the run, and the mutexes."""

    def __init__(self, workers, *, runtime, made=None):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ducktest")
        self._runtime = runtime
        self._made = made or Made()
        self._mine = threading.local()
        self._shared = {}  # identity -> the run's instance, for an engine of lifetime `run`
        self._engines = []  # every instance made, so that the run can close them all
        self._mutexes = {}
        self._running = {}  # test id -> {engine id: Engine}, while it runs
        self._guard = threading.Lock()

    def submit(self, fn, *args):
        return self._pool.submit(fn, *args)

    def take(self, test_id, definitions):
        """The test's engine instances, acquired: one of lifetime `worker` is this worker's instance of
        that identity, one of lifetime `run` the run's, each made when it is first needed and warm
        after; one of lifetime `batch` is made for this test. Registered under `test_id` until
        `give_back`, so a timeout noticed on another thread can reach them (`cancel`)."""
        mine = getattr(self._mine, "engines", None)
        if mine is None:
            mine = self._mine.engines = {}
        taken = {}
        try:
            for d in definitions:
                engine = d.make(self._made.inputs_of(d), run_token=self._made.run_token)
                engine.runtime = self._runtime
                with self._guard:
                    if d.lifetime == WORKER:
                        engine = mine.setdefault(engine.identity, engine)
                    elif d.lifetime == RUN:
                        engine = self._shared.setdefault(engine.identity, engine)
                    if engine not in self._engines:
                        self._engines.append(engine)
                engine.acquire(RW)
                taken[d.id] = engine
        except BaseException:
            self.give_back(test_id, taken, failed=True)
            raise
        with self._guard:
            self._running[test_id] = taken
        return taken

    def give_back(self, test_id, engines, *, failed):
        with self._guard:
            self._running.pop(test_id, None)
        for engine in engines.values():
            engine.release(engine, RW, failed=failed)
            if engine.definition.lifetime == BATCH:
                engine.close()

    def cancel(self, test_id):
        """A test's timeout expired: cancel its jobs where the engine can, and close its engine
        instances, so a statement stuck in one releases as an exception and the next test opens
        them again. The worker's thread is not touched -- Python cannot kill it -- and keeps running
        whatever it was doing until that releases it too."""
        with self._guard:
            engines = self._running.get(test_id, {})
        for engine in engines.values():
            live = engine.live
            try:
                if hasattr(live, "cancel_jobs"):
                    live.cancel_jobs(test_id)
                engine.close()
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
        """Nothing that is still waiting starts, and every engine instance this pool made is closed
        -- which is what ends the statement a body of a stopped run is waiting for."""
        self._pool.shutdown(wait=False, cancel_futures=True)
        for engine in list(self._engines):
            try:
                engine.close()
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
