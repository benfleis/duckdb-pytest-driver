"""plan -> provision -> exec -> cleanup -> report, over the items `scan` selected.

Each phase writes what it decided into the run dir (`01-tests.yaml`, `05-report.yaml`), so a run can
be inspected after the fact. The plan is serial and provisioning is a temp dir per test plus the
engines the selected tests declare; both grow only when a test needs more.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import shutil
import sys
import time
import traceback

import yaml

from . import sqllogic
from .binary import UsageError
from .client import SetupFailed
from .ctx import Ctx
from .engines.duckdb import DuckDBSession
from .mnemonic import run_id as make_run_id
from .model import SQLLOGIC
from .provision import KEPT, attach_argument, existing_overrides, provision, release

# FAILED: something under test went wrong. ERROR: setup or teardown did, so the test proved nothing.
PASSED, FAILED, ERROR, BLOCKED, SKIPPED = "passed", "failed", "error", "blocked", "skipped"


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


def _run_body(case, ctx):
    """Returns (outcome, [problem descriptions]). Checks that failed under on_fail="continue" are all
    reported; an abort or a crash is reported after them; teardowns run whatever happened, Ctrl-C too."""
    body, outcome, problems = case.test.body, PASSED, []
    try:
        try:
            body(ctx)
        except SetupFailed as exc:
            outcome, problems = ERROR, [f"setup failed\n{_describe_failure(exc, body)}"]
        except Exception as exc:
            outcome, problems = FAILED, [_describe_failure(exc, body)]
        if ctx.failed_checks:
            problems = [_describe_failure(f, body) for f in ctx.failed_checks] + problems
            outcome = FAILED if outcome == PASSED else outcome
    finally:
        for teardown in reversed(ctx.teardowns):
            try:
                teardown()
            except Exception as exc:
                problems.append(f"teardown failed\n{exc}")
                outcome = ERROR if outcome == PASSED else outcome
    return outcome, problems


def _env_for_body(case, provisioned):
    """What a `.test` body sees as `{NAME}`: the case's values, upper-cased as a .test writes them."""
    return {k.upper(): v for k, v in case.values.items()}


def _init_sql(case, provisioned):
    parts = []
    for v in case.axis_values.values():
        sql = v.init_sql({sid: p.block for sid, p in provisioned.items()}) if callable(v.init_sql) else v.init_sql
        if sql:
            parts.append(sql.strip().rstrip(";") + ";")
    return "\n".join(parts)


def _batch_key(case, provisioned):
    return json.dumps(
        [
            sorted(_env_for_body(case, provisioned).items()),
            sorted(case.settings.items()),
            _init_sql(case, provisioned),
            bool(case.services),
        ],
        default=str,
    )


def run(
    cases,
    project,
    *,
    duckdb_bin,
    unittest_bin=None,
    working_dir=".",
    temp_root,
    keep=False,
    existing_services=(),
    keep_services=False,
    batch_size=10,
    workers=4,
    out=sys.stdout,
):
    engines = project.engines
    run_id = make_run_id()
    run_dir = os.path.join(os.path.abspath(temp_root), run_id)
    os.makedirs(run_dir)
    _write(run_dir, "01-tests.yaml", [c.record(project) for c in cases])
    events = open(os.path.join(run_dir, "04-events.jsonl"), "w")
    results = []

    def done(case, outcome, *, runner, seconds=None, problems=(), kept=(), note="", **more):
        result = {"id": case.id, "outcome": outcome, "runner": runner}
        if seconds is not None:
            result["seconds"] = seconds
        if problems:
            result["problems"] = list(problems)
        if kept:
            result["kept"] = list(kept)
        if note:
            result["note"] = note
        result.update(more)
        results.append(result)
        events.write(
            json.dumps(
                {**result, "variants": {a: v.id for a, v in case.axis_values.items()}, "case": case.case_id},
                default=str,
            )
            + "\n"
        )
        events.flush()
        tail = (
            f"  ({seconds}s"
            + (
                ": " + ", ".join(f"{k} {v}s" for k, v in more.get("seconds_by_client", {}).items())
                if more.get("seconds_by_client")
                else ""
            )
            + ")"
            if seconds is not None
            else ""
        )
        print(f"{outcome.upper():8} {case.id}{tail}" + (f"  [{note}]" if note else ""), file=out, flush=True)

    # provision: each service some selected case needs, then each engine. One that does not come up
    # blocks its cases; the rest of the run goes on.
    provisioned, boot_errors = {}, {}
    for service_id in sorted({s for c in cases for s in c.services}):
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

    blocked = lambda c: [x for x in (*c.services, *c.engines) if x in boot_errors]  # noqa: E731
    handles = {}
    # An engine boots only when a case that can still run needs it.
    for engine_id in sorted({e for c in cases if not blocked(c) for e in c.engines}):
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

    runnable = []
    for case in cases:
        if by := blocked(case):
            done(case, BLOCKED, runner="-", blocked_by=by, note=f"{by[0]} did not come up")
        else:
            runnable.append(case)

    duckdb_session = DuckDBSession(duckdb_bin)
    try:
        # .test bodies with no Python: batches through the unittest binary, several at a time.
        sqllogic_cases = [c for c in runnable if c.test.type == SQLLOGIC]
        if sqllogic_cases:
            if unittest_bin is None:
                raise UsageError("the selection holds .test files, and no unittest binary was found")
            groups = {}
            for c in sqllogic_cases:
                groups.setdefault(_batch_key(c, provisioned), []).append(c)
            batches = []
            for group in groups.values():
                slow = [c for c in group if "slow" in c.tags]
                fast = [c for c in group if "slow" not in c.tags]
                for chunk in [fast[i : i + batch_size] for i in range(0, len(fast), batch_size)] + [[c] for c in slow]:
                    n = len(batches) + 1
                    first = chunk[0]
                    batches.append(
                        (
                            chunk,
                            sqllogic.Batch(
                                os.path.join(run_dir, "batches", f"b{n:03d}"),
                                [c.test.body_file for c in chunk],
                                _env_for_body(first, provisioned),
                                first.settings,
                                _init_sql(first, provisioned),
                                strict_connection_errors=bool(first.services),
                                timeouts={c.test.body_file: c.test.timeout for c in chunk if c.test.timeout},
                            ),
                        )
                    )
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {
                    pool.submit(sqllogic.run_batch, unittest_bin, batch, working_dir): (chunk, batch)
                    for chunk, batch in batches
                }
                for future in concurrent.futures.as_completed(futures):
                    chunk, batch = futures[future]
                    by_name = future.result()
                    for case in chunk:
                        r = by_name[case.test.body_file]
                        problems = [r.detail] if r.detail and r.outcome != PASSED else []
                        done(
                            case,
                            r.outcome,
                            runner="unittest",
                            seconds=r.seconds,
                            problems=problems,
                            kept=[r.kept] if r.kept else [],
                            note=r.note,
                            batch=os.path.relpath(batch.dir, run_dir),
                        )
                    if all(by_name[c.test.body_file].outcome == PASSED for c in chunk) and not keep:
                        shutil.rmtree(batch.dir, ignore_errors=True)

        # Python bodies, in this process, one after another.
        for case in runnable:
            if case.test.type == SQLLOGIC:
                continue
            temp_dir = os.path.join(run_dir, "cases", case.dir_name)
            os.makedirs(temp_dir)
            duckdb_session.begin_test()
            for handle in handles.values():
                if hasattr(handle, "begin_test"):
                    handle.begin_test()
            ctx = Ctx(
                case,
                temp_dir=temp_dir,
                duckdb_bin=duckdb_bin,
                duckdb_session=duckdb_session,
                engines=handles,
                services=provisioned,
                remote_parts=(run_id, case.dir_name),
                init_sql=_init_sql(case, provisioned),
                run_test=lambda case=case, temp_dir=temp_dir, **kw: _run_paired(
                    case, unittest_bin, working_dir, temp_dir, provisioned, **kw
                ),
            )
            t0 = time.perf_counter()
            outcome, problems = _run_body(case, ctx)
            kept = []
            if outcome == PASSED and not keep:
                shutil.rmtree(temp_dir)
                for service_id, uri in ctx.remote_locations:
                    provisioned[service_id].service.clean(provisioned[service_id].block, uri)
            else:
                kept = [temp_dir, *(uri for _, uri in ctx.remote_locations)]
            done(
                case,
                outcome,
                runner="python",
                seconds=round(time.perf_counter() - t0, 2),
                problems=problems,
                kept=kept,
                seconds_by_client=ctx.seconds_by_client(),
            )
    finally:
        events.close()
        duckdb_session.close()
        for handle in handles.values():
            handle.stop()
        for p in provisioned.values():
            release(p)
            if p.mode == KEPT:
                print(f"service {p.service.id} left running; next run: {attach_argument(p)}", file=out)

    outcomes = (PASSED, FAILED, ERROR, BLOCKED, SKIPPED)
    counts = {o: sum(r["outcome"] == o for r in results) for o in outcomes}
    _write(
        run_dir,
        "05-report.yaml",
        {"run_id": run_id, "counts": counts, "not_provisioned": boot_errors, "cases": results},
    )

    for r in results:
        if r["outcome"] in (FAILED, ERROR):
            print(f"\n--- {r['id']}", file=out)
            for problem in r.get("problems", ()):
                print(f"{problem}\n", file=out)
            if r.get("kept"):
                print("  kept: " + "\n        ".join(r["kept"]), file=out)
            print(f"  again: ducktest5 run --id {shlex.quote(r['id'])}", file=out)
    skipped = [r for r in results if r["outcome"] == SKIPPED]
    if skipped:
        print(f"\n{len(skipped)} skipped inside the binary:", file=out)
        by_reason = {}
        for r in skipped:
            by_reason.setdefault((r.get("problems") or ["no reason given"])[0], []).append(r["id"])
        for reason, ids in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(ids):4}  {reason}", file=out)
            for i in ids[:3]:
                print(f"          {i}", file=out)
            if len(ids) > 3:
                print(f"          ... and {len(ids) - 3} more", file=out)
    for name, error in boot_errors.items():
        n_blocked = sum(name in r.get("blocked_by", ()) for r in results)
        print(f"\n{name!r} did not come up: {n_blocked} cases blocked\n  {error}", file=out)
    summary = ", ".join(f"{counts[o]} {o}" for o in outcomes if counts[o] or o == PASSED)
    print(f"\n{summary} -- {run_dir}", file=out)
    # Green means it ran: a skip inside the binary does not fail the run, anything else not passed does,
    # and a run in which nothing passed is not green.
    bad = counts[FAILED] + counts[ERROR] + counts[BLOCKED]
    return 0 if bad == 0 and counts[PASSED] > 0 else 1


def _run_paired(case, unittest_bin, working_dir, temp_dir, provisioned, *, env=None, init_sql=""):
    """`ctx.run_test()`: the `.test` file beside a Python body, as a batch of one."""
    if unittest_bin is None:
        raise UsageError(f"{case.id} runs {case.test.body_file}, and no unittest binary was found")
    batch = sqllogic.Batch(
        os.path.join(temp_dir, "run_test"),
        [case.test.body_file],
        {**_env_for_body(case, provisioned), **(env or {})},
        case.settings,
        "\n".join(x for x in (_init_sql(case, provisioned), init_sql) if x),
        case.test.timeout or sqllogic.DEFAULT_TIMEOUT_S,
        strict_connection_errors=bool(case.services),
    )
    result = sqllogic.run_batch(unittest_bin, batch, working_dir)[case.test.body_file]
    if result.outcome == SKIPPED:
        raise SetupFailed(f"{case.test.body_file} was skipped inside the binary: {result.detail}")
    if result.outcome != PASSED:
        raise AssertionError(f"{case.test.body_file}\n{result.detail}")
