"""plan -> provision -> exec -> cleanup -> report, over the items `scan` selected.

Each phase writes what it decided into the run dir (`01-tests.yaml`, `05-report.yaml`), so a run can
be inspected after the fact. The plan is serial and provisioning is a temp dir per test plus the
engines the selected tests declare; both grow only when a test needs more.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback

import yaml

from .ctx import Ctx
from .mnemonic import run_id as make_run_id

PASSED, FAILED, BLOCKED = "passed", "failed", "blocked"


def _write(run_dir, name, doc):
    with open(os.path.join(run_dir, name), "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, width=100)


def _describe_failure(exc, body):
    """The error, plus the innermost line of the test's own file: where the body failed, not where a
    helper raised."""
    text = "".join(traceback.format_exception_only(type(exc), exc)).rstrip()
    lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename == body.__code__.co_filename]
    return f"{text}\n  at {body.__code__.co_filename}:{lines[-1]}" if lines else text


def run(items, engines, *, duckdb_bin, temp_root, keep=False, out=sys.stdout):
    run_id = make_run_id()
    run_dir = os.path.join(os.path.abspath(temp_root), run_id)
    os.makedirs(run_dir)
    _write(run_dir, "01-tests.yaml", [i.record(engines) for i in items])

    # provision: boot each engine some selected test declares, once. A failed boot blocks its tests.
    handles, boot_errors = {}, {}
    for engine_id in sorted({e for i in items for e in i.test.engines}):
        log = os.path.join(run_dir, f"engine-{engine_id}.log")
        print(f"boot {engine_id} ({engines[engine_id].variant}) ...", file=out, flush=True)
        t0 = time.perf_counter()
        try:
            handles[engine_id] = engines[engine_id].boot(log)
            print(f"boot {engine_id} -- done ({time.perf_counter() - t0:.1f}s)", file=out, flush=True)
        except Exception as exc:
            boot_errors[engine_id] = f"{type(exc).__name__}: {exc} (log: {log})"

    results = []
    try:
        for n, item in enumerate(items):
            blocked_by = [e for e in item.test.engines if e in boot_errors]
            if blocked_by:
                results.append({"id": item.id, "outcome": BLOCKED, "blocked_by": blocked_by})
                print(f"BLOCKED  {item.id}  (engine {blocked_by[0]} did not boot)", file=out, flush=True)
                continue
            temp_dir = os.path.join(run_dir, f"{n:03d}")
            os.makedirs(temp_dir)
            ctx = Ctx(item, temp_dir=temp_dir, duckdb_bin=duckdb_bin, engines=handles)
            t0 = time.perf_counter()
            try:
                item.test.body(ctx)
                result = {"id": item.id, "outcome": PASSED}
            except Exception as exc:
                result = {"id": item.id, "outcome": FAILED, "error": _describe_failure(exc, item.test.body)}
            result["seconds"] = round(time.perf_counter() - t0, 2)
            if result["outcome"] == PASSED and not keep:
                shutil.rmtree(temp_dir)
            else:
                result["kept"] = temp_dir
            results.append(result)
            print(f"{result['outcome'].upper():8} {item.id}  ({result['seconds']}s)", file=out, flush=True)
    finally:
        for handle in handles.values():
            handle.stop()

    counts = {o: sum(r["outcome"] == o for r in results) for o in (PASSED, FAILED, BLOCKED)}
    _write(run_dir, "05-report.yaml", {"run_id": run_id, "counts": counts, "engines_failed": boot_errors, "tests": results})

    for r in results:
        if r["outcome"] == FAILED:
            print(f"\n--- {r['id']}\n{r['error']}\n  kept: {r['kept']}", file=out)
    for engine_id, error in boot_errors.items():
        blocked = sum(engine_id in r.get("blocked_by", ()) for r in results)
        print(f"\nengine {engine_id!r} did not boot: {blocked} tests blocked\n  {error}", file=out)
    print(f"\n{counts[PASSED]} passed, {counts[FAILED]} failed, {counts[BLOCKED]} blocked -- {run_dir}", file=out)
    return 0 if counts[FAILED] == counts[BLOCKED] == 0 else 1
