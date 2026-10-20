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
one, whichever is ready for it first -- and take them in plan order. What a test holds, it takes in
one order for the whole run, as mutexes are: its named locks, sorted, then its engine instances,
sorted by identity. So no two tests can each hold what the other waits for. Two engines of one
identity named by one test are two instances: each identity's instances are taken in the order
they were made.

A Python body that passes its timeout is given up on: what it holds is released, and a new thread
takes its slot. Python cannot kill a thread; the old one is a daemon, left to end on its own, and
never holds up the next test or the end of the process.
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import threading
import time

from .resources import RW
from .resources.engine import BATCH, RUN, WORKER
from .resources.made import Made

JOIN_GRACE_S = 2.0  # how long join() waits for a thread that is still running a test


class Revoked(Exception):
    """The test's timeout passed while it waited to take something: it holds nothing any more."""


class Lease:
    """What one test holds while it runs: the locks of its mutexes and its engine instances."""

    def __init__(self, test_id):
        self.test_id = test_id
        self.locks = []
        self.engines = {}
        self.revoked = False


class Pool:
    """The `--workers` slots, the engine instances of each worker and of the run, and the mutexes."""

    def __init__(self, workers, *, runtime, made=None):
        self._runtime = runtime
        self._made = made or Made()
        self._mine = threading.local()
        self._shared = {}  # identity -> the run's instances of it, for an engine of lifetime `run`
        self._engines = []  # every instance made, so that the run can close them all
        self._mutexes = {}
        self._leases = {}  # test id -> Lease, while it runs
        self._guard = threading.Lock()
        self._tasks = collections.deque()
        self._ready = threading.Condition(self._guard)
        self._closed = False
        self._threads = set()
        self._given_up = set()  # threads whose test passed its timeout: they end with it
        self._running = {}  # test id -> the thread that runs it
        for _ in range(max(1, workers)):
            self._start_thread()

    # -- the slots ------------------------------------------------------------
    def _start_thread(self):
        n = len(self._threads)
        thread = threading.Thread(target=self._work, name=f"ducktest-{n}", daemon=True)
        thread.worker = n  # read off the thread object, not reparsed from its name, everywhere a worker number is needed
        self._threads.add(thread)
        thread.start()

    def submit(self, fn, *args, owner=None):
        """`fn(*args)` in the next free slot, in the order submitted. `owner` is the test it runs,
        so that `revoke` can find its thread."""
        future = concurrent.futures.Future()
        with self._ready:
            self._tasks.append((future, fn, args, owner))
            self._ready.notify()
        return future

    def _work(self):
        me = threading.current_thread()
        while True:
            with self._ready:
                self._ready.wait_for(lambda: self._tasks or self._closed)
                if not self._tasks:
                    return
                future, fn, args, owner = self._tasks.popleft()
                if owner is not None:
                    self._running[owner] = me
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args))
                except BaseException as exc:
                    future.set_exception(exc)
            with self._guard:
                if owner is not None and self._running.get(owner) is me:
                    del self._running[owner]
                if me in self._given_up:
                    self._threads.discard(me)
                    return

    # -- what a test holds ----------------------------------------------------
    @contextlib.contextmanager
    def lease(self, test_id):
        lease = Lease(test_id)
        with self._guard:
            self._leases[test_id] = lease
        try:
            yield lease
        finally:
            with self._guard:
                if self._leases.get(test_id) is lease:
                    del self._leases[test_id]

    def _keep(self, lease, held, release, engine_id=None):
        """Records on `lease` what was just taken, unless the test was given up on meanwhile: then
        it is released at once."""
        with self._guard:
            if lease is None or not lease.revoked:
                if lease is not None and engine_id is None:
                    lease.locks.append(held)
                elif lease is not None:
                    lease.engines[engine_id] = held
                return
        release()
        raise Revoked(lease.test_id)

    @contextlib.contextmanager
    def holding(self, names, lease=None):
        """The named locks, taken in sorted order and released in the other one."""
        taken = []
        try:
            for name in sorted(set(names)):
                with self._guard:
                    lock = self._mutexes.setdefault(name, threading.Lock())
                lock.acquire()
                self._keep(lease, lock, lock.release)
                taken.append(lock)
            yield
        finally:
            with self._guard:
                gone = lease is not None and lease.revoked  # `revoke` released them
                if lease is not None:
                    lease.locks.clear()
            if not gone:
                for lock in reversed(taken):
                    lock.release()

    def take(self, lease, definitions):
        """The test's engine instances, acquired: one of lifetime `worker` is this worker's instance of
        that identity, one of lifetime `run` the run's, each made when it is first needed and warm
        after; one of lifetime `batch` is made for this test. Registered on `lease` until
        `give_back`, so a timeout noticed on another thread can reach them (`revoke`)."""
        mine = getattr(self._mine, "engines", None)
        if mine is None:
            mine = self._mine.engines = {}
        chosen, seen = [], collections.Counter()
        for d in definitions:
            engine = d.make(self._made.inputs_of(d), run_token=self._made.run_token)
            engine.runtime = self._runtime
            n = seen[engine.identity]
            seen[engine.identity] += 1
            with self._guard:
                if d.lifetime in (WORKER, RUN):
                    instances = (mine if d.lifetime == WORKER else self._shared).setdefault(engine.identity, [])
                    if n == len(instances):
                        instances.append(engine)
                    engine = instances[n]
                if engine not in self._engines:
                    self._engines.append(engine)
            chosen.append((engine.identity, n, d.name, engine))
        taken = {}
        try:
            for _, _, engine_id, engine in sorted(chosen, key=lambda c: c[:2]):
                engine.holder = lease.test_id if lease is not None else None
                engine.acquire(RW)
                self._keep(lease, engine, lambda e=engine: e.release(e, RW, failed=True), engine_id)
                taken[engine_id] = engine
        except BaseException:
            self.give_back(lease, taken, failed=True)
            raise
        return {engine_id: taken[engine_id] for _, _, engine_id, _ in chosen}

    def give_back(self, lease, engines, *, failed):
        with self._guard:
            if lease.revoked:
                return  # `revoke` released them
            for engine_id in engines:
                lease.engines.pop(engine_id, None)
        for engine in engines.values():
            engine.release(engine, RW, failed=failed)
            if engine.definition.lifetime == BATCH:
                engine.close()

    def worker_of(self, test_id):
        """The worker running `test_id`'s task (its `owner` on `submit`), while the pool still knows
        it: set when the task is dequeued, cleared when it returns -- so a timed-out test's
        abandoned thread is still found here right up to `revoke`."""
        with self._guard:
            thread = self._running.get(test_id)
        return getattr(thread, "worker", None) if thread is not None else None

    def revoke(self, test_id):
        """A test's timeout expired: cancel its jobs where the engine can, close and release its
        engine instances, release its mutexes, and give its slot to a new thread. Its own thread is
        not touched -- Python cannot kill it -- and keeps running whatever it was doing; what it
        gives back later is ignored."""
        with self._guard:
            lease = self._leases.get(test_id)
            thread = self._running.get(test_id)
            if lease is None or lease.revoked:
                return
            lease.revoked = True
            engines, locks = list(lease.engines.values()), list(lease.locks)
            if thread is not None and thread not in self._given_up:
                self._given_up.add(thread)
                self._start_thread()
        for engine in engines:
            try:
                if hasattr(engine.live, "cancel_jobs"):
                    engine.live.cancel_jobs(test_id)
                engine.release(engine, RW, failed=True)
            except Exception:
                pass
        for lock in reversed(locks):
            lock.release()

    def stop(self):
        """Nothing that is still waiting starts, and every engine instance this pool made is closed
        -- which is what ends the statement a body of a stopped run is waiting for."""
        with self._ready:
            self._closed = True
            while self._tasks:
                self._tasks.popleft()[0].cancel()
            self._ready.notify_all()
        for engine in list(self._engines):
            try:
                engine.close()
            except Exception:
                pass

    def join(self, timeout=JOIN_GRACE_S):
        """What still runs, run out, for up to `timeout`. A thread whose test was given up on is not
        waited for; neither is one still running past `timeout`: they are daemons, and the run ends
        without them."""
        deadline = time.monotonic() + timeout
        with self._guard:
            threads = [t for t in self._threads if t not in self._given_up]
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
