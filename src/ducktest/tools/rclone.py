"""Generic rclone runner — the object-store seed/clean mechanism (P3; see docs/SERVICES.md).

Object stores get the SQL lane's two-phase, access-moded shape: **DCL** (create the bucket/container) +
**DML** (sync the dataset in), with ``access="ro"|"rw"`` deciding isolation — the same ``@requires``
vocabulary. rclone is the uniform driver across azurite / real Azure / S3 (the remote abstracts the
backend), so this module is generic; a *service* supplies only its :class:`Remote` params (backend
knowledge), e.g. azurite's ``{"type": "azureblob", "use_emulator": True}``.

Two ways to address a remote:
  - **inline** (file-free, hermetic): ``:azureblob,use_emulator=true:container/prefix`` — used for the
    automated path. Safe only when param VALUES carry no ``,``/``=``/``/`` (fine for ``use_emulator``;
    a real account KEY would mangle it — use a config file for those).
  - **config file** (the human affordance + the secret-bearing path): a named ``[remote]`` stanza; verbs
    take ``config=<path>`` and address it as ``name:container/prefix``. :func:`write_conf` dumps one.

Verbs are thin ``rclone`` subprocess wrappers that raise on failure with the captured tail. The actual
calls need a live endpoint (Ben's-box/docker verified); the serialization + prefix logic is offline-pure.
"""

import subprocess
from dataclasses import dataclass, field


def _v(x):
    """Serialize a param value the rclone way: booleans lowercase, everything else str()."""
    if x is True:
        return "true"
    if x is False:
        return "false"
    return str(x)


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
    cmd = ["rclone", *(["--config", config] if config else []), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = " | ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise RuntimeError(f"rclone {args[0]} failed: {detail}")
    return proc.stdout


def mkdir(remote, path="", *, config=None):
    """DCL: ensure a container/bucket (or path) exists. Idempotent."""
    _run(["mkdir", remote.target(path, config=config)], config=config)


def sync(src, remote, path="", *, config=None):
    """DML: make the remote path mirror local ``src`` (uploads new/changed, deletes extra)."""
    _run(["sync", src, remote.target(path, config=config)], config=config)


def purge(remote, path="", *, config=None):
    """Clean: delete the remote path and its contents."""
    _run(["purge", remote.target(path, config=config)], config=config)


def check(src, remote, path="", *, config=None):
    """Verify the remote path matches local ``src`` (the cheap ro re-seed guard). Raises on mismatch."""
    _run(["check", src, remote.target(path, config=config)], config=config)


def ls(remote, path="", *, config=None):
    """List objects under the remote path (returns rclone's stdout)."""
    return _run(["ls", remote.target(path, config=config)], config=config)


def seed(remote, src, container, name, *, access, token=None, config=None):
    """DCL + DML in one: ensure ``container`` (mkdir), sync ``src`` into the access-scoped prefix, return it.

    ``ro`` seeds the shared ``container/name``; ``rw`` seeds the per-test ``container/<token>/name``.
    Callers build the backend URI (e.g. ``az://<prefix>``) from the returned prefix + the service block.
    """
    prefix = object_prefix(container, name, access=access, token=token)
    mkdir(remote, container, config=config)  # DCL
    sync(src, remote, prefix, config=config)  # DML
    return prefix
