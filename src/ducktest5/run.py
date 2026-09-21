"""provision -> exec -> cleanup -> report, over the cases a selection gave.

`.test` cases go to the unittest binary in batches, several processes at a time; Python bodies run in
this process, one after another. What was decided and what happened is in the run dir:
`01-tests.yaml`, `03-provision.yaml` (secrets redacted), `04-events.jsonl` (one line for each case),
`05-report.yaml`. Whatever goes wrong outside a body is an ERROR of the cases it hit, never the end
of the run, and services are released on every way out.

With `debugger` (`--lldb`) the one selected `.test` case is prepared as usual -- services, batch dir,
`config.json`, init file -- and then handed to the debugger in the foreground instead of being run.
The batch dir stays behind for a second look, or a second debugger.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import shutil
import subprocess
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
from .outcomes import ALL as OUTCOMES
from .outcomes import BLOCKED, ERROR, FAILED, PASSED, SKIPPED
from .provision import KEPT, MASK, attach_argument, existing_overrides, provision, release


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
        except UsageError as exc:
            outcome, problems = (
                ERROR,
                [f"usage error of the test, not of the command line\n{_describe_failure(exc, body)}"],
            )
        except SetupFailed as exc:
            outcome, problems = ERROR, [f"setup failed\n{_describe_failure(exc, body)}"]
        except Exception as exc:
            outcome, problems = FAILED, [_describe_failure(exc, body)]
        if ctx.failed_checks:
            problems = [_describe_failure(f, body) for f in ctx.failed_checks] + problems
            outcome = FAILED  # also over a setup that failed later: the check had already proven a failure
    finally:
        for teardown in reversed(ctx.teardowns):
            try:
                teardown()
            except Exception as exc:
                problems.append(f"teardown failed\n{exc}")
                outcome = ERROR if outcome == PASSED else outcome
    return outcome, problems


def _redacted(value, secrets):
    if isinstance(value, dict):
        return {k: _redacted(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, secrets) for v in value]
    text = value if isinstance(value, str) else str(value)
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


def _made_of(case, provisioned, *, batch_dir=None, case_dir=None):
    """What a failed case was made of, for a reader who did not write it. The init SQL is named by
    its file, never quoted: it may hold a secret."""
    secrets = {str(p.block[k]) for p in provisioned.values() for k in p.service.secrets if p.block.get(k)}
    facts = {}
    if case.values:
        facts["values"] = _redacted(case.values, secrets)
    if case.settings:
        facts["settings"] = _redacted(case.settings, secrets)
    if case.axis_values:
        facts["variants"] = [
            {"axis": axis, "value": v.id, **({"settings": _redacted(v.settings, secrets)} if v.settings else {})}
            for axis, v in case.axis_values.items()
        ]
    if case.services:
        facts["services"] = {s: provisioned[s].mode if s in provisioned else "did not come up" for s in case.services}
    files = {}
    if batch_dir:
        files = {
            "config": os.path.join(batch_dir, "config.json"),
            "tests": os.path.join(batch_dir, "tests.txt"),
            "init": os.path.join(batch_dir, sqllogic.INIT_FILE),
            "output": os.path.join(batch_dir, "output.txt"),
        }
    if case_dir:
        files = {"dir": case_dir}
    files = {name: path for name, path in files.items() if os.path.exists(path)}
    if files:
        facts["files"] = files
    return facts


def _print_made_of(facts, out):
    if values := facts.get("values"):
        print("  values: " + ", ".join(f"{k}={v}" for k, v in values.items()), file=out)
    if settings := facts.get("settings"):
        print("  settings: " + ", ".join(f"{k}={v}" for k, v in settings.items()), file=out)
    for variant in facts.get("variants", ()):
        of_its_own = variant.get("settings")
        tail = "  (" + ", ".join(f"{k}={v}" for k, v in of_its_own.items()) + ")" if of_its_own else ""
        print(f"  variant: {variant['axis']}={variant['value']}{tail}", file=out)
    for service_id, mode in facts.get("services", {}).items():
        print(f"  service: {service_id} ({mode})", file=out)
    for name, path in facts.get("files", {}).items():
        print(f"  {name}: {path}", file=out)


def _env_for_body(case):
    """What a `.test` body sees as `{NAME}`: the case's values, upper-cased as a .test writes them."""
    return {k.upper(): v for k, v in case.values.items()}


def _init_parts(case, provisioned):
    """(what gave it, SQL) for each axis value that brings init SQL, in the order of the axes."""
    parts = []
    for axis, v in case.axis_values.items():
        sql = v.init_sql({sid: p.block for sid, p in provisioned.items()}) if callable(v.init_sql) else v.init_sql
        if sql:
            parts.append((f"{axis}={v.id}", sql.strip().rstrip(";") + ";"))
    return parts


def _init_sql(case, provisioned):
    return "\n".join(sql for _, sql in _init_parts(case, provisioned))


def _init_from(case, provisioned):
    return ", ".join(label for label, _ in _init_parts(case, provisioned))


def _only_one_test_case(cases):
    """A debugger takes over the terminal, so there is one process to take over."""
    if len(cases) != 1:
        listed = [c.id for c in cases[:10]] + ([f"... and {len(cases) - 10} more"] if len(cases) > 10 else [])
        raise UsageError(
            f"a debugger needs exactly one .test case; the selection gives {len(cases)}:\n  " + "\n  ".join(listed)
        )
    if cases[0].test.type != SQLLOGIC:
        raise UsageError(f"a debugger runs the unittest binary; {cases[0].id} is a Python body -- not supported yet")


def _batch_key(case, provisioned):
    return json.dumps(
        [
            sorted(_env_for_body(case).items()),
            sorted(case.settings.items()),
            _init_sql(case, provisioned),
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
    out=None,
    session_factory=DuckDBSession,
    debugger=None,
):
    out = out or sys.stdout  # looked up now: a caller may have replaced it since import
    if debugger is not None:
        _only_one_test_case(cases)
    engines = project.engines
    run_id = make_run_id()
    run_dir = os.path.join(os.path.abspath(temp_root), run_id)
    os.makedirs(run_dir)
    _write(run_dir, "01-tests.yaml", [c.record(project) for c in cases])
    events = open(os.path.join(run_dir, "04-events.jsonl"), "w")
    results = []

    def done(
        case, outcome, *, runner, seconds=None, problems=(), kept=(), note="", batch_dir=None, case_dir=None, **more
    ):
        result = {"id": case.id, "outcome": outcome, "runner": runner}
        if seconds is not None:
            result["seconds"] = seconds
        if problems:
            result["problems"] = list(problems)
        if outcome in (FAILED, ERROR):
            result["made_of"] = _made_of(case, provisioned, batch_dir=batch_dir, case_dir=case_dir)
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

    provisioned, boot_errors, handles, debugged = {}, {}, {}, False
    duckdb_home = os.path.join(run_dir, "home")
    duckdb_session = session_factory(duckdb_bin, home=duckdb_home)
    blocked = lambda c: [x for x in (*c.services, *c.engines) if x in boot_errors]  # noqa: E731
    try:
        # provision: each service some selected case needs, then each engine. One that does not come
        # up blocks its cases; the rest of the run goes on.
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
        _write(
            run_dir, "03-provision.yaml", {k: {"mode": p.mode, "block": p.redacted()} for k, p in provisioned.items()}
        )

        # An engine boots only when a case that can still run needs it.
        for engine_id in sorted({e for c in cases if not blocked(c) for e in c.engines}):
            log = os.path.join(run_dir, f"engine-{engine_id}.log")
            print(f"boot {engine_id} ({engines[engine_id].version}) ...", file=out, flush=True)
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

        # .test bodies with no Python: batches through the unittest binary, several at a time.
        sqllogic_cases = [c for c in runnable if c.test.type == SQLLOGIC]
        if sqllogic_cases and unittest_bin is None:
            raise UsageError("the selection holds .test files, and no unittest binary was found")
        groups = {}
        for c in sqllogic_cases:
            groups.setdefault(_batch_key(c, provisioned), []).append(c)
        batches = []
        for group in groups.values():
            # One file once in a batch: the binary reports by file name. A case with a service runs
            # alone: a failure in a batch of several runs again for its text, and that second run would
            # see what the first left on the service.
            alone = lambda c: "slow" in c.tags or bool(c.services)  # noqa: E731
            chunks, slow = [], [[c] for c in group if alone(c)]
            for c in (c for c in group if not alone(c)):
                chunk = next(
                    (
                        k
                        for k in chunks
                        if len(k) < batch_size and c.test.body_file not in {x.test.body_file for x in k}
                    ),
                    None,
                )
                if chunk is None:
                    chunks.append(chunk := [])
                chunk.append(c)
            for chunk in chunks + slow:
                first = chunk[0]
                batches.append(
                    (
                        chunk,
                        sqllogic.Batch(
                            os.path.join(run_dir, "batches", f"b{len(batches) + 1:03d}"),
                            [(c.id, c.test.body_file) for c in chunk],
                            _env_for_body(first),
                            first.settings,
                            _init_sql(first, provisioned),
                            timeouts={c.id: c.test.timeout for c in chunk if c.test.timeout},
                            retries={c.id: c.test.retries for c in chunk if c.test.retries},
                            init_from=_init_from(first, provisioned),
                        ),
                    )
                )
        if debugger is not None and batches:
            ((_, batch),) = batches
            batch.write()
            args = shlex.split(debugger) + sqllogic.batch_args(unittest_bin, batch)
            print("\n" + " ".join(shlex.quote(a) for a in args) + "\n", file=out, flush=True)
            debugged = True
            code = subprocess.call(args, cwd=working_dir)  # the terminal is the debugger's
            print(f"\nbatch kept: {batch.dir}", file=out, flush=True)
            if batch.init_sql:
                print(
                    f"  {sqllogic.INIT_FILE} there may hold a secret; remove the directory when you are done", file=out
                )
            return code

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(sqllogic.run_batch, unittest_bin, batch, working_dir): (chunk, batch)
                for chunk, batch in batches
            }
            for future in concurrent.futures.as_completed(futures):
                chunk, batch = futures[future]
                try:
                    by_case = future.result()
                except Exception as exc:  # the binary could not be started, the batch dir not written, ...
                    for case in chunk:
                        done(
                            case,
                            ERROR,
                            runner="unittest",
                            problems=[f"the batch could not run: {type(exc).__name__}: {exc}"],
                            batch_dir=batch.dir,
                        )
                    continue
                for case in chunk:
                    r = by_case[case.id]
                    problems = [r.detail] if r.detail and r.outcome != PASSED else []
                    done(
                        case,
                        r.outcome,
                        runner="unittest",
                        seconds=r.seconds,
                        problems=problems,
                        kept=[r.kept] if r.kept else [],
                        note=r.note,
                        batch_dir=batch.dir,
                        batch=os.path.relpath(batch.dir, run_dir),
                        **({"attempts": r.attempts} if r.attempts > 1 else {}),
                    )
                # What a failed attempt printed stays, also when a later attempt passed.
                if keep:
                    continue
                if all(r.outcome == PASSED and r.attempts == 1 for r in by_case.values()):
                    shutil.rmtree(batch.dir, ignore_errors=True)
                else:
                    sqllogic.remove_init_sql(batch.dir)

        # Python bodies, in this process, one after another.
        def attempt(case, n):
            """Returns (outcome, problems, the case's directory, what it may have written on services)."""
            # An attempt after the first has its own directory and prefix, so it does not see what
            # the one before it left.
            dir_name = case.dir_name if n == 1 else f"{case.dir_name}.attempt{n}"
            temp_dir = os.path.join(run_dir, "cases", dir_name)
            try:
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
                    duckdb_home=duckdb_home,
                    engines=handles,
                    services=provisioned,
                    remote_parts=(run_id, dir_name),
                    init_sql=_init_sql(case, provisioned),
                    run_test=lambda **kw: _run_paired(case, unittest_bin, working_dir, temp_dir, provisioned, **kw),
                )
            except Exception as exc:
                return ERROR, [f"the case could not be set up: {type(exc).__name__}: {exc}"], temp_dir, None
            return *_run_body(case, ctx), temp_dir, ctx

        for case in runnable:
            if case.test.type == SQLLOGIC:
                continue
            t0 = time.perf_counter()
            allowed, tried, first = 1 + case.test.retries, [], None
            for n in range(1, allowed + 1):
                outcome, problems, temp_dir, ctx = attempt(case, n)
                tried.append((temp_dir, ctx))
                if outcome not in (FAILED, ERROR):
                    break
                first = first or (outcome, problems)
            note = ""
            if outcome in (FAILED, ERROR):
                outcome, problems = first
            elif n > 1:
                note = f"passed on attempt {n} of {allowed}"
            kept = []
            if outcome == PASSED and not keep:
                for temp_dir, _ in tried:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                for service_id, uri in (x for _, c in tried if c for x in c.remote_locations):
                    try:
                        provisioned[service_id].service.clean(provisioned[service_id].block, uri)
                    except Exception as exc:
                        outcome = ERROR
                        problems.append(
                            f"what the case wrote at {uri} could not be removed: {type(exc).__name__}: {exc}"
                        )
                        kept.append(uri)
            else:
                kept = [
                    *(temp_dir for temp_dir, _ in tried if os.path.isdir(temp_dir)),
                    *(uri for _, c in tried if c for _, uri in c.remote_locations),
                ]
                if not keep:
                    for temp_dir, _ in tried:
                        sqllogic.remove_init_sql(temp_dir)
            done(
                case,
                outcome,
                runner="python",
                seconds=round(time.perf_counter() - t0, 2),
                problems=problems,
                kept=kept,
                note=note,
                case_dir=tried[0][0],
                seconds_by_client=ctx.seconds_by_client() if ctx else {},
                **({"attempts": n} if n > 1 else {}),
            )
    finally:
        events.close()
        duckdb_session.close()
        for handle in handles.values():
            try:
                handle.stop()
            except Exception:
                pass
        for p in provisioned.values():
            try:
                release(p)
            except Exception as exc:
                print(f"service {p.service.id} could not be stopped: {exc}", file=out)
            if p.mode == KEPT:
                print(f"service {p.service.id} left running; next run: {attach_argument(p)}", file=out)
        if not debugged:  # the debugger ran the case, so nothing here has an outcome to report
            _report(run_dir, run_id, results, boot_errors, out)

    counts = {o: sum(r["outcome"] == o for r in results) for o in OUTCOMES}
    # Green means it ran: a skip inside the binary does not fail the run, anything else not passed does,
    # and a run in which nothing passed is not green.
    bad = counts[FAILED] + counts[ERROR] + counts[BLOCKED]
    return 0 if bad == 0 and counts[PASSED] > 0 else 1


def _report(run_dir, run_id, results, boot_errors, out):
    counts = {o: sum(r["outcome"] == o for r in results) for o in OUTCOMES}
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
            _print_made_of(r.get("made_of", {}), out)
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
    summary = ", ".join(f"{counts[o]} {o}" for o in OUTCOMES if counts[o] or o == PASSED)
    print(f"\n{summary} -- {run_dir}", file=out)


def _run_paired(case, unittest_bin, working_dir, temp_dir, provisioned, *, env=None, init_sql=""):
    """`ctx.run_test()`: the `.test` file beside a Python body, as a batch of one."""
    if unittest_bin is None:
        raise UsageError(f"{case.id} runs {case.test.body_file}, and no unittest binary was found")
    batch = sqllogic.Batch(
        os.path.join(temp_dir, "run_test"),
        [(case.id, case.test.body_file)],
        {**_env_for_body(case), **(env or {})},
        case.settings,
        "\n".join(x for x in (_init_sql(case, provisioned), init_sql) if x),
        case.test.timeout or sqllogic.DEFAULT_TIMEOUT_S,
        init_from=", ".join(
            x for x in (_init_from(case, provisioned), "run_test(init_sql=...)" if init_sql else "") if x
        ),
    )
    result = sqllogic.run_batch(unittest_bin, batch, working_dir)[case.id]
    if result.outcome == SKIPPED:
        raise SetupFailed(f"{case.test.body_file} was skipped inside the binary: {result.detail}")
    if result.outcome != PASSED:
        raise AssertionError(f"{case.test.body_file}\n{result.detail}")
