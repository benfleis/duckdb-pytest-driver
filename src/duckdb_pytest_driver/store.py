"""Process-shared state store backing driver resource provisioning.

A :class:`SyncManager` server (started on the xdist controller, pre-fork) holds
whole-block values plus **per-key** provision locks; workers connect over a
platform-native socket (AF_UNIX on posix, AF_PIPE on Windows). Access verbs,
by consumer intent::

    put(store, key, block)              # owner writes a block (the controller, pre-fork)

    copy(store, key)                    # eager consumer: a private copy, or fail loud
        present -> block                #   (pre-filled by the controller pre-fork)
      | absent  -> ResourceMissing      #   never provisions; never blocks

    copy_or_provision(store, key, fn)   # lazy consumer: a private copy, or provision once
        cached  -> block
      | miss    -> single-flight (first caller runs fn + publishes; others block)
      | timeout -> ProvisionTimeout     #   stuck/dead winner, no hang

Portability: the default start context is used (spawn on macOS/Windows, fork on
Linux), so this module must stay **import-clean** — the spawned server re-imports
it. Security is not a goal (a local-only socket; an authkey is used only because
multiprocessing requires one).

Values are whole **blocks** (a credential ``{TOKEN, ENDPOINT, …}`` or a service
``{name, url, …}``), stored **JSON-serialized** — so a block is a str that can't be
aliased or mutated in place, and every read hands back a freshly-parsed private copy.
"""

import json
import os
import threading
from multiprocessing.managers import SyncManager

ADDR_ENV = "DUCKDB_PYTEST_STORE_ADDR"
KEY_ENV = "DUCKDB_PYTEST_STORE_AUTHKEY"


class ResourceMissing(Exception):
    """An eager resource was expected pre-filled but absent — fail loud, never skip."""


class ProvisionTimeout(Exception):
    """Blocked waiting on a provision that never arrived (stuck/dead winner)."""


class _Store:
    """Server-side singleton: whole-block KV + per-key locks (created on demand)."""

    def __init__(self):
        self._values = {}
        self._locks = {}
        self._meta = threading.Lock()  # guards _locks creation

    def get(self, key):  # returns the serialized block (str), or None if absent
        return self._values.get(key)

    def put(self, key, value):  # value is a serialized block (str); wholesale replace
        self._values[key] = value

    def _lock_for(self, key):
        with self._meta:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    def acquire(self, key, timeout=None):
        lock = self._lock_for(key)
        return lock.acquire() if timeout is None else lock.acquire(timeout=timeout)

    def release(self, key):
        self._locks[key].release()


_STORE_SINGLETON = None


def _get_store():
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        _STORE_SINGLETON = _Store()
    return _STORE_SINGLETON


class StoreManager(SyncManager):
    pass


# one shared _Store for every client that connects (per-key locks live inside it)
StoreManager.register("store", callable=_get_store, exposed=["get", "put", "acquire", "release"])


# --- lifecycle -------------------------------------------------------------


def start_server():
    """Controller, pre-fork: start the manager. Returns (manager, address, authkey).

    The caller stashes address+authkey (see :func:`to_env`) so workers can connect.
    """
    authkey = os.urandom(16)
    mgr = StoreManager(address=None, authkey=authkey)  # native family; default (spawn-safe) ctx
    mgr.start()
    return mgr, mgr.address, authkey


def connect(address, authkey):
    """Any process: connect to a running server and return the manager."""
    mgr = StoreManager(address=address, authkey=authkey)
    mgr.connect()
    return mgr


# --- env delivery (address is a str on posix/windows; only AF_INET is a tuple) ---


def _encode_addr(address):
    return f"inet\t{address[0]}\t{address[1]}" if isinstance(address, tuple) else f"native\t{address}"


def _decode_addr(encoded):
    kind, _, rest = encoded.partition("\t")
    if kind == "inet":
        host, port = rest.split("\t")
        return (host, int(port))
    return rest


def to_env(address, authkey):
    """Env vars carrying the server location — set on the controller pre-fork so
    workers (which inherit env at spawn) can :func:`from_env` + :func:`connect`."""
    return {ADDR_ENV: _encode_addr(address), KEY_ENV: authkey.hex()}


def from_env(env=None):
    """Read (address, authkey) published by :func:`to_env`; None if not published."""
    env = os.environ if env is None else env
    if ADDR_ENV not in env or KEY_ENV not in env:
        return None
    return _decode_addr(env[ADDR_ENV]), bytes.fromhex(env[KEY_ENV])


# --- access verbs (copy = require-present; copy_or_provision = provide-if-missing) ---


def put(store, key, block):
    """Owner writes ``block`` (e.g. the controller pre-filling a credential, pre-fork).

    Stored JSON-serialized, so the shared value is an un-aliasable str.
    """
    store.put(key, json.dumps(block))


def copy(store, key, missing=None):
    """Eager consumer: return a private copy of ``key``'s block, or fail loud if absent.

    For a resource the controller must have pre-provisioned before workers run
    (e.g. a credential) — never provisions, never blocks; an absent value is a real
    failure, not a skip (raises :class:`ResourceMissing`).
    """
    raw = store.get(key)
    if raw is None:
        raise ResourceMissing(missing or f"required resource {key!r} was not provisioned")
    return json.loads(raw)


def copy_or_provision(store, key, factory, timeout=None):
    """Lazy consumer: return a private copy of ``key``'s block, or provision it once.

    ``factory`` returns a block and runs in THIS process (first-need-wins) under the
    key's provision lock; concurrent callers block, then read what the winner
    published. ``timeout`` bounds the wait — a stuck/dead winner raises
    ProvisionTimeout rather than hanging forever.
    """
    raw = store.get(key)
    if raw is not None:
        return json.loads(raw)
    acquired = store.acquire(key) if timeout is None else store.acquire(key, timeout)
    if not acquired:
        raise ProvisionTimeout(f"timed out after {timeout}s waiting to provision {key!r}")
    try:
        raw = store.get(key)  # loser re-reads: the winner published while we blocked
        if raw is not None:
            return json.loads(raw)
        block = factory()  # winner only
        store.put(key, json.dumps(block))
        return block
    finally:
        store.release(key)
