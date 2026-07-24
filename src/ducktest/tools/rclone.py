"""Generic rclone runner — the object-store seed/clean mechanism (P3; see docs/SERVICES.md).

Object stores get the SQL lane's two-phase, access-moded shape: **DCL** (create the bucket/container) +
**DML** (sync the dataset in), with ``access="ro"|"rw"`` deciding isolation — the same ``@requires``
vocabulary. rclone is the uniform driver across azurite / real Azure / S3 (the remote abstracts the
backend), so this module is generic; a *service* supplies only its :class:`Remote` params (backend
knowledge), e.g. azurite's ``{"type": "azureblob", "use_emulator": True}``.

Two ways to address a remote:
  - **inline** (file-free, hermetic): ``:azureblob,use_emulator=true:container/prefix`` — the default,
    automated path. Only safe when param VALUES carry no ``,``/``=``/``/``/``:`` (fine for
    ``use_emulator``; a real account KEY or a non-default endpoint URL always mangles it).
  - **config file** (the human affordance + the secret-bearing path): a named ``[remote]`` stanza; verbs
    take ``config=<path>`` and address it as ``name:container/prefix``. :func:`write_conf` dumps one.

Every verb picks between these **automatically** (`_effective_config`) — inline when the remote's
params allow it, else a temp config file, cleaned up after the call. A caller never needs to know or
check which happened; pass ``config=`` explicitly only to use a specific persistent conf file (e.g. the
human-affordance dump from ``provision-service --seed``).

Verbs are thin ``rclone`` subprocess wrappers that raise on failure with the captured tail. The actual
calls need a live endpoint (Ben's-box/docker verified); the serialization + prefix logic is offline-pure.
"""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field


def _v(x):
    """Serialize a param value the rclone way: booleans lowercase, everything else str()."""
    if x is True:
        return "true"
    if x is False:
        return "false"
    return str(x)


# Characters the inline `:type,k=v,...:path` form can't carry in a value (see Remote.inline's
# docstring): `,`/`=` collide with the k=v/list separators; `/`/`:` collide with rclone's own
# remote:path addressing. A real account key (base64: `/`, `=`) or a non-default endpoint URL
# (`:`, `/`) always trips this -- found live via a reverse-port-mapped azurite instance, where
# _populate's inline mkdir/sync silently failed with "no Host in request URL".
_UNSAFE_INLINE_CHARS = (",", "=", "/", ":")


def _inline_safe(remote) -> bool:
    """Whether `remote`'s params can address safely via `Remote.inline()` -- False if any non-`type`
    value carries a character the inline syntax can't carry (a real key or a custom endpoint always
    will; `use_emulator=true`-style defaults never do)."""
    return not any(any(c in _UNSAFE_INLINE_CHARS for c in _v(v)) for k, v in remote.params.items() if k != "type")


@contextmanager
def _effective_config(remote, config):
    """The config path a verb should actually use: the caller's explicit `config` verbatim, or a
    temp file auto-written (via `write_conf`) when `remote`'s inline form isn't safe, or `None` to
    stay inline (the common, local/default-emulator case). Callers should never need to know which
    happened -- this is the fix for `_inline_safe` returning False, applied transparently."""
    if config is not None or _inline_safe(remote):
        yield config
        return
    fd, path = tempfile.mkstemp(prefix="ducktest-rclone-", suffix=".conf")
    os.close(fd)
    write_conf(remote, path)
    try:
        yield path
    finally:
        os.unlink(path)


@dataclass(frozen=True)
class Remote:
    """An rclone remote = a name + backend params (``type`` required). Data only; a service ships it."""

    name: str
    params: dict = field(default_factory=dict)

    def inline(self, path=""):
        """``:type,k=v,…:path`` — the file-free address. Values must be ``,``/``=``-free (see module doc)."""
        p = dict(self.params)
        typ = p.pop("type", None)
        if not typ:
            raise ValueError(f"rclone Remote {self.name!r}: params must include a 'type'")
        parts = [typ] + [f"{k}={_v(v)}" for k, v in p.items()]
        return ":" + ",".join(parts) + ":" + path

    def stanza(self):
        """The ``[name]\\ntype = …`` rclone.conf block for this remote."""
        lines = [f"[{self.name}]"] + [f"{k} = {_v(v)}" for k, v in self.params.items()]
        return "\n".join(lines) + "\n"

    def target(self, path="", *, config=None):
        """The rclone remote-path token: ``name:path`` when a config file backs it, else the inline form."""
        return f"{self.name}:{path}" if config else self.inline(path)


def write_conf(remotes, path):
    """Write ``remotes`` (one or many :class:`Remote`) to an rclone.conf at ``path``; return ``path``.

    The human affordance behind ``provision-service --seed``: a named remote to poke at by hand
    (``rclone --config <path> ls azurite:container``). Same params as the automated inline path.
    """
    if isinstance(remotes, Remote):
        remotes = [remotes]
    with open(path, "w") as f:
        f.write("\n".join(r.stanza() for r in remotes))
    return path


def object_prefix(container, name, *, access, token=None):
    """The object-store path for a dataset, isolated by access mode (mirrors the SQL cell-schema split).

    ``ro`` => ``container/name`` (shared, seeded once). ``rw`` => ``container/<token>/name`` (per-test,
    collision-free under xdist — the SAME provision token that namespaces cell-schemas). ``rw`` requires a
    token.
    """
    if access == "rw":
        if not token:
            raise ValueError("object_prefix: rw access requires a token (per-test isolation)")
        return f"{container}/{token}/{name}"
    if access == "ro":
        return f"{container}/{name}"
    raise ValueError(f"object_prefix: access must be 'ro' or 'rw', got {access!r}")


# --- verbs (thin `rclone` wrappers) --------------------------------------------------------


def _run(args, config=None):
    # Bound every call. Without these, an unreachable/mis-endpointed remote spins on rclone's defaults
    # (--contimeout 1m x --low-level-retries 10 x --retries 3 = minutes PER verb) — a seed round-trip
    # then hangs the docker tier instead of failing fast. A local emulator connects in ms, so tight
    # bounds cost nothing on success and turn a multi-minute hang into a seconds-fast, informative error.
    bounds = ["--contimeout=10s", "--timeout=30s", "--retries=1", "--low-level-retries=2"]
    cmd = ["rclone", *bounds, *(["--config", config] if config else []), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = " | ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise RuntimeError(f"rclone {args[0]} failed: {detail}")
    return proc.stdout


def mkdir(remote, path="", *, config=None):
    """DCL: ensure a container/bucket (or path) exists. Idempotent."""
    with _effective_config(remote, config) as cfg:
        _run(["mkdir", remote.target(path, config=cfg)], config=cfg)


def sync(src, remote, path="", *, config=None):
    """DML: make the remote path mirror local ``src`` (uploads new/changed, deletes extra)."""
    with _effective_config(remote, config) as cfg:
        _run(["sync", src, remote.target(path, config=cfg)], config=cfg)


def purge(remote, path="", *, config=None):
    """Clean: delete the remote path and its contents."""
    with _effective_config(remote, config) as cfg:
        _run(["purge", remote.target(path, config=cfg)], config=cfg)


def check(src, remote, path="", *, config=None):
    """Verify the remote path matches local ``src`` (the cheap ro re-seed guard). Raises on mismatch."""
    with _effective_config(remote, config) as cfg:
        _run(["check", src, remote.target(path, config=cfg)], config=cfg)


def ls(remote, path="", *, config=None):
    """List objects under the remote path (returns rclone's stdout)."""
    with _effective_config(remote, config) as cfg:
        return _run(["ls", remote.target(path, config=cfg)], config=cfg)


def seed(remote, src, container, name, *, access, token=None, config=None):
    """DCL + DML in one: ensure ``container`` (mkdir), sync ``src`` into the access-scoped prefix, return it.

    ``ro`` seeds the shared ``container/name``; ``rw`` seeds the per-test ``container/<token>/name``.
    Callers build the backend URI (e.g. ``az://<prefix>``) from the returned prefix + the service block.
    """
    prefix = object_prefix(container, name, access=access, token=token)
    mkdir(remote, container, config=config)  # DCL
    sync(src, remote, prefix, config=config)  # DML
    return prefix
