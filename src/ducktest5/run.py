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

from .client import SetupFailed
from .ctx import Ctx
from .engines.duckdb import DuckDBSession
from .mnemonic import run_id as make_run_id
from .provision import KEPT, attach_argument, existing_overrides, provision, release

# FAILED: something under test went wrong. ERROR: setup or teardown did, so the test proved nothing.
PASSED, FAILED, ERROR, BLOCKED = "passed", "failed", "error", "blocked"


def _write(run_dir, name, doc):
    with open(os.path.join(run_dir, name), "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, width=100)


def _describe_failure(exc, body):
    """The error, plus the innermost line of the test's own file: where the body failed, not where a
    helper raised."""
    text = "".join(traceback.format_exception_only(type(exc), exc)).rstrip()
    stack = getattr(exc, "stack", None) or traceback.extract_tb(exc.__traceback__)
    lines = [f.lineno for f in stack if f.filename == body.__code__.co_filename]
    return f"{text}\n  at {body.__code__.co_filename}:{lines[-1]}" if lines else text


def _run_body(item, ctx):
    """Returns (outcome, [problem descriptions]). Checks that failed are all reported; an abort or a
    crash is reported after them; teardowns run whatever happened."""
    body, outcome, problems = item.test.body, PASSED, []
    try:
        body(ctx)
    except SetupFailed as exc:
        outcome, problems = ERROR, [f"setup failed\n{_describe_failure(exc, body)}"]
    except Exception as exc:
        outcome, problems = FAILED, [_describe_failure(exc, body)]
    if ctx.failed_checks:
        problems = [_describe_failure(f, body) for f in ctx.failed_checks] + problems
        outcome = FAILED if outcome == PASSED else outcome
    for teardown in reversed(ctx.teardowns):
        try:
            teardown()
        except Exception as exc:
            problems.append(f"teardown failed\n{exc}")
            outcome = ERROR if outcome == PASSED else outcome
    return outcome, problems


def run(
    items, project, *, duckdb_bin, temp_root, keep=False, existing_services=(), keep_services=False, out=sys.stdout
):
    engines = project.engines
    run_id = make_run_id()
    run_dir = os.path.join(os.path.abspath(temp_root), run_id)
    os.makedirs(run_dir)
    _write(run_dir, "01-tests.yaml", [i.record(engines) for i in items])

    # provision: each service some selected test declares, then each engine. One that does not come
    # up blocks its tests; the rest of the run goes on.
    provisioned, boot_errors = {}, {}
    for service_id in sorted({s for i in items for s in i.test.services}):
        print(f"service {service_id} ...", file=out, flush=True)
        try:
            provisioned[service_id] = provision(
                project.services[service_id],
                existing=existing_overrides(service_id, existing_services),
                keep=keep_services,
            )
            print(f"service {service_id} -- {provisioned[service_id].mode}", file=out, flush=True)
        except Exception as exc:
            boot_errors[service_id] = f"{type(exc).__name__}: {exc}"
    _write(run_dir, "03-provision.yaml", {k: {"mode": p.mode, "block": p.redacted()} for k, p in provisioned.items()})

    handles = {}
    # An engine boots only when a test that can still run needs it.
    runnable = [i for i in items if not any(s in boot_errors for s in i.test.services)]
    for engine_id in sorted({e for i in runnable for e in i.test.engines}):
        log = os.path.join(run_dir, f"engine-{engine_id}.log")
        print(f"boot {engine_id} ({engines[engine_id].variant}) ...", file=out, flush=True)
        t0 = time.perf_counter()
        try:
            handles[engine_id] = engines[engine_id].boot(log)
            for service_id in engines[engine_id].uses:
                if service_id in provisioned:
                    handles[engine_id].use_service(service_id, provisioned[service_id].block)
            print(f"boot {engine_id} -- done ({time.perf_counter() - t0:.1f}s)", file=out, flush=True)
        except Exception as exc:
            boot_errors[engine_id] = f"{type(exc).__name__}: {exc} (log: {log})"

    results = []
    duckdb_session = DuckDBSession(duckdb_bin)
    try:
        for n, item in enumerate(items):
            blocked_by = [x for x in (*item.test.services, *item.test.engines) if x in boot_errors]
            if blocked_by:
                results.append({"id": item.id, "outcome": BLOCKED, "blocked_by": blocked_by})
                print(f"BLOCKED  {item.id}  ({blocked_by[0]} did not come up)", file=out, flush=True)
                continue
            temp_dir = os.path.join(run_dir, f"{n:03d}")
            os.makedirs(temp_dir)
            duckdb_session.begin_test()
            ctx = Ctx(
                item,
                temp_dir=temp_dir,
                duckdb_bin=duckdb_bin,
                duckdb_session=duckdb_session,
                engines=handles,
                services=provisioned,
                remote_parts=(run_id, f"{n:03d}"),
            )
            t0 = time.perf_counter()
            outcome, problems = _run_body(item, ctx)
            result = {"id": item.id, "outcome": outcome}
            if problems:
                result["problems"] = problems
            result["seconds"] = round(time.perf_counter() - t0, 2)
            result["seconds_by_client"] = ctx.seconds_by_client()
            result["seconds_by_client"] = ctx.seconds_by_client()
            if result["outcome"] == PASSED and not keep:
                shutil.rmtree(temp_dir)
                for service_id, uri in ctx.remote_locations:
                    provisioned[service_id].service.clean(provisioned[service_id].block, uri)
            else:
                result["kept"] = [temp_dir, *(uri for _, uri in ctx.remote_locations)]
            results.append(result)
            spent = ", ".join(f"{name} {seconds}s" for name, seconds in result["seconds_by_client"].items())
            print(f"{result['outcome'].upper():8} {item.id}  ({result['seconds']}s: {spent})", file=out, flush=True)
    finally:
        duckdb_session.close()
        for handle in handles.values():
            handle.stop()
        for p in provisioned.values():
            release(p)
            if p.mode == KEPT:
                print(f"service {p.service.id} left running; next run: {attach_argument(p)}", file=out)

    counts = {o: sum(r["outcome"] == o for r in results) for o in (PASSED, FAILED, ERROR, BLOCKED)}
    _write(
        run_dir,
        "05-report.yaml",
        {"run_id": run_id, "counts": counts, "not_provisioned": boot_errors, "tests": results},
    )

    for r in results:
        if r["outcome"] in (FAILED, ERROR):
            print(f"\n--- {r['id']}", file=out)
            for problem in r["problems"]:
                print(f"{problem}\n", file=out)
            print("  kept: " + "\n        ".join(r["kept"]), file=out)
    for name, error in boot_errors.items():
        blocked = sum(name in r.get("blocked_by", ()) for r in results)
        print(f"\n{name!r} did not come up: {blocked} tests blocked\n  {error}", file=out)
    summary = ", ".join(f"{counts[o]} {o}" for o in (PASSED, FAILED, ERROR, BLOCKED) if counts[o] or o == PASSED)
    print(f"\n{summary} -- {run_dir}", file=out)
    return 0 if counts[PASSED] == len(results) else 1
