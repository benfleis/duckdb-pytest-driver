"""Binary-authoritative collection: FS ∪ `unittest -l`, reconciled — the false-green killer.

The shipped driver collected `.test` files off the FILESYSTEM only. But the duckdb `unittest` binary
has its OWN registered set (via `unittest -l`), and the two differ: `.test_slow`/`.test_coverage`,
`third_party/sqllogictest`, extension `_deps` are registered in the binary but the FS gate never
walks/accepts them — so they silently DON'T run and the suite is green anyway. That false-green was
the "100% trust" blocker, addressed in the old plan as a bolt-on verify pass *someday*.

Here the binary is a first-class gather source from day one, with two modes:
  - verify (default):  gather both, HARD-ERROR on divergence. Green now means the FS view and the
                       binary's registered set agree — no silently-uncollected test can hide.
  - authoritative:     collect `binary ∪ FS`, dedup by name; the binary's set is truth.

This module owns: (1) the member/role model (a `.py` can be a driver, a body, or both — roles, not
file types), (2) listing the binary's registered tests, (3) the reconcile, (4) batching contiguous
same-binary items into `xdist_group`s. Item *creation* lives in sqllogic.py; the plan-level reconcile
result is recorded on `Plan.fs_names`/`binary_names` by the controller.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Iterable, Optional

# --- the member/role model (roles are not file types) -----------------------------------------

_BODY_SUFFIXES = (".test",)  # `.test_slow`/`.test_coverage` recognized separately, behind --slow
_SLOW_SUFFIXES = (".test_slow", ".test_coverage")


def has_driver(test_path: str) -> bool:
    """A `.test` body with a same-stem `.py` beside it → runs only via that driver (body suppressed)."""
    return os.path.exists(_sibling(test_path, ".py"))


def is_driver(py_path: str) -> bool:
    """A `.py` with a same-stem `.test` beside it → a driver whose fixtures wrap the body."""
    return os.path.exists(_sibling(py_path, ".test"))


def _sibling(path: str, suffix: str) -> str:
    return os.path.splitext(path)[0] + suffix


def is_body(path: str, *, slow: bool = False) -> bool:
    sfx = _BODY_SUFFIXES + (_SLOW_SUFFIXES if slow else ())
    return path.endswith(sfx)


# --- listing the binary's registered set ------------------------------------------------------


def list_binary_tests(binary: str, *, working_dir: str, extra_args: Optional[list[str]] = None) -> frozenset[str]:
    """The names the `unittest` binary itself has registered — the authoritative corpus.

    Names are returned relative to `working_dir` (the same convention SqlLogicItem uses as its
    name-filter), so they compare directly with the FS gather. A binary that doesn't support `-l`
    (too old) raises — a session invariant we want to fail-fast on, not paper over.
    """
    cmd = [binary, "-l", *(extra_args or [])]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=working_dir)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = " | ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
        raise RuntimeError(f"`unittest -l` failed (is the binary too old to list tests?): {detail}")
    return frozenset(_parse_listing(proc.stdout, working_dir))


def _parse_listing(out: str, working_dir: str) -> Iterable[str]:
    """One test name per non-empty line (Catch2 `-l` listing). Normalize to working-dir-relative."""
    for raw in out.splitlines():
        name = raw.strip()
        if not name or name.startswith(("=", "-", "All available", "tags")):
            continue  # skip Catch2 listing chrome
        # a listing may print absolute or repo-relative; normalize to the item name-filter form
        if os.path.isabs(name):
            name = os.path.relpath(name, working_dir)
        yield name


# --- the reconcile (verify vs authoritative) --------------------------------------------------


@dataclass(frozen=True)
class Reconcile:
    fs_names: frozenset[str]
    binary_names: frozenset[str]
    mode: str  # "verify" | "authoritative"

    @property
    def only_in_binary(self) -> frozenset[str]:
        """Registered in the binary but the FS never collected → the false-green set."""
        return self.binary_names - self.fs_names

    @property
    def only_in_fs(self) -> frozenset[str]:
        """Walked on disk but the binary doesn't know it (a stray/renamed `.test`, or a driverless
        body the binary registers under a different name) → worth surfacing, usually a real mistake."""
        return self.fs_names - self.binary_names

    @property
    def collected_names(self) -> frozenset[str]:
        """What actually runs. authoritative = union; verify = FS (having proven no divergence)."""
        return (self.fs_names | self.binary_names) if self.mode == "authoritative" else self.fs_names

    def check(self) -> None:
        """verify mode: hard-error on ANY divergence — the structural false-green guard.

        This is the north star at collection time: a selected corpus that doesn't match the binary's
        truth is a failure, not a quietly-smaller green run.
        """
        if self.mode != "verify":
            return
        miss_bin = sorted(self.only_in_binary)
        miss_fs = sorted(self.only_in_fs)
        if miss_bin or miss_fs:
            parts = []
            if miss_bin:
                parts.append(
                    "registered in the binary but NOT collected off disk (would be false-green):\n  "
                    + "\n  ".join(miss_bin[:50])
                    + (f"\n  … and {len(miss_bin) - 50} more" if len(miss_bin) > 50 else "")
                )
            if miss_fs:
                parts.append(
                    "collected off disk but the binary doesn't register (stale/renamed?):\n  "
                    + "\n  ".join(miss_fs[:50])
                )
            raise RuntimeError(
                "collection divergence (FS vs `unittest -l`) — refusing a false-green run.\n"
                + "\n\n".join(parts)
                + "\n\nFix the mismatch, or run with --collect-source=authoritative to collect the union."
            )


def reconcile(fs_names: Iterable[str], binary_names: Iterable[str], *, mode: str) -> Reconcile:
    return Reconcile(frozenset(fs_names), frozenset(binary_names), mode)


# --- batching (contiguous same-binary/workdir runs → one xdist_group) -------------------------


def assign_batches(items: list, *, batch_size: int) -> int:
    """Group contiguous `SqlLogicItem`s sharing (binary, working_dir) into batches of `batch_size`,
    stamping each with a `_batch_id` + an `xdist_group("sqllogic_batch_N")` marker so `--dist=
    loadgroup` keeps the whole batch on ONE worker (which is what makes the per-worker batch result
    cache coherent). Returns the number of batches. Non-SqlLogic items are passed through untouched.

    Ordering is collection (FS) order; batching relies on that adjacency, so nothing re-sorts items.
    Slow bodies are batch-1 (scheduled alone) — set by the caller marking them, honored here.
    """
    import pytest

    batch_no = 0
    i = 0
    n = len(items)
    while i < n:
        it = items[i]
        key = _batch_key(it)
        if key is None:
            i += 1
            continue
        # a slow item batches alone
        cap = 1 if getattr(it, "_slow", False) else batch_size
        run = [it]
        j = i + 1
        while j < n and len(run) < cap and _batch_key(items[j]) == key and not getattr(items[j], "_slow", False):
            run.append(items[j])
            j += 1
        names = [x._test_name for x in run]
        for x in run:
            x._batch_id = batch_no
            x._batch_test_names = names
            x.add_marker(pytest.mark.xdist_group(f"sqllogic_batch_{batch_no}"))
        batch_no += 1
        i = j
    return batch_no


def _batch_key(item) -> Optional[tuple]:
    """The affinity key: same binary + same working dir batch together. None = not a SqlLogic item."""
    b = getattr(item, "_binary", None)
    w = getattr(item, "_working_dir", None)
    if b is None or w is None:
        return None
    return (b, w)
