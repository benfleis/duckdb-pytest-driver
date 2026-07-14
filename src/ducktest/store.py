"""Process-shared state store backing driver resource provisioning.

A :class:`SyncManager` server (started on the xdist controller, pre-fork) holds
whole-block values plus a **per-key provisioning state machine**; workers connect
over a platform-native socket (AF_UNIX on posix, AF_PIPE on Windows). Access verbs,
by consumer intent::

    put(store, key, block)              # owner writes a block (the controller, pre-fork)

    copy(store, key)                    # eager consumer: a private copy, or fail loud
        present -> block                #   (pre-filled by the controller pre-fork)
      | absent  -> ResourceMissing      #   never provisions; never blocks

    copy_or_provision(store, key, fn)   # lazy consumer: a private copy, or provision once
        set     -> block                #   write-once/read-many per-key state machine
      | miss    -> single-flight (owner runs fn + publishes; others block on PENDING)
      | failed  -> ProvisionFailed      #   poison pill: owner's fn raised; waiters fail fast, no retry
      | timeout -> ProvisionTimeout     #   stuck/never-terminating owner, no hang

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
import time
from multiprocessing.managers import SyncManager

ADDR_ENV = "DUCKDB_PYTEST_STORE_ADDR"
KEY_ENV = "DUCKDB_PYTEST_STORE_AUTHKEY"


class ResourceMissing(Exception):
    """An eager resource was expected pre-filled but absent — fail loud, never skip."""


class ProvisionTimeout(Exception):
    """Blocked waiting on a provision that never arrived (stuck/never-terminating owner)."""


class ProvisionFailed(Exception):
    """A provision terminally FAILED (the poison pill): the owner's factory raised, so the key is
    marked failed and every subsequent reader fails fast — no factory re-run, no retry storm."""


# per-key provisioning state — write-once, read-many: absent -> PENDING -> SET | FAILED.
_PENDING, _SET, _FAILED = "pending", "set", "failed"


class _Store:
    """Server-side singleton: whole-block KV + a per-key provisioning state machine.

    ``begin`` atomically claims PENDING (no cross-process lock needed); SET is terminal-success (the
    block is readable), FAILED is terminal-failure (a poison pill). Waiters poll ``begin`` until a key
    reaches a terminal state.
    """

    def __init__(self):
        self._values = {}  # key -> serialized block (str), present only when SET
        self._errors = {}  # key -> error message, present only when FAILED
        self._state = {}  # key -> _PENDING | _SET | _FAILED  (absent => never started)
        self._meta = threading.Lock()

    def get(self, key):  # eager read: the SET block (str), or None if not SET
        with self._meta:
            return self._values.get(key)

    def put(self, key, value):  # direct write (eager creds): terminal SET
        with self._meta:
            self._values[key] = value
            self._state[key] = _SET

    def begin(self, key):
        """Atomically claim provisioning. Returns (role, payload):
        ("owner", None)   -> you claimed PENDING; run the factory, then set()/fail().
        ("set", value)    -> already provisioned; use value.
        ("failed", error) -> terminal failure; poison pill.
        ("wait", None)    -> another caller is PENDING; poll begin() again.
        """
        with self._meta:
            st = self._state.get(key)
            if st is None:
                self._state[key] = _PENDING
                return ("owner", None)
            if st == _SET:
                return ("set", self._values[key])
            if st == _FAILED:
                return ("failed", self._errors.get(key, "provision failed"))
            return ("wait", None)

    def set(self, key, value):
        with self._meta:
            self._values[key] = value
            self._state[key] = _SET

    def fail(self, key, error):
        with self._meta:
            self._errors[key] = error
            self._state[key] = _FAILED


_STORE_SINGLETON = None


def _get_store():
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        _STORE_SINGLETON = _Store()
    return _STORE_SINGLETON


class StoreManager(SyncManager):
    pass


# one shared _Store for every client that connects (the state machine lives inside it)
StoreManager.register("store", callable=_get_store, exposed=["get", "put", "begin", "set", "fail"])


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


def copy_or_provision(store, key, factory, timeout=None, poll=0.2):
    """Lazy consumer: return a private copy of ``key``'s block, or provision it once.

    Write-once/read-many via the per-key state machine: the first caller (``owner``) runs ``factory``
    and publishes the block; concurrent callers block (poll) until it's ``set``, or **fail fast** if
    the owner's factory raised (``failed`` — the poison pill: no factory re-run, no retry storm).
    ``timeout`` bounds a waiter's poll for a stuck / never-terminating owner (``ProvisionTimeout``).
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        role, payload = store.begin(key)
        if role == "set":
            return json.loads(payload)
        if role == "failed":
            raise ProvisionFailed(payload)
        if role == "owner":
            try:
                block = factory()  # owner only
            except BaseException as e:
                store.fail(key, str(e) or repr(e))  # poison the key for every waiter
                raise
            store.set(key, json.dumps(block))
            return block
        # role == "wait": another caller is provisioning — poll until terminal or timeout
        if deadline is not None and time.monotonic() >= deadline:
            raise ProvisionTimeout(f"timed out after {timeout}s waiting to provision {key!r}")
        time.sleep(poll)
