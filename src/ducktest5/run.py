"""exec: the plan's batches, run, as one event for each case; and `run`, which is every phase.

`ducktest5 run` is `scan`, `plan`, `provision`, `exec`, `cleanup` and `report` in one process, and
each of them is the function the subcommand of the same name calls (`phases.py`). What was decided
and what happened is in the run dir: `01-tests.yaml`, `02-plan.yaml`, `03-provision.yaml`,
`04-events.jsonl` (one line for each case), `05-report.yaml` and `05-report.json`. Whatever goes
wrong outside a body is an ERROR of the cases it hit, never the end of the run, and services are
released on every way out.

`.test` batches go to the unittest binary, several processes at a time, or to DuckDB's own `run`
wrapper (`--runner run`, `runners.py`); Python bodies run in worker threads of this process. Both
kinds take their slot from one budget of `--workers`, and what each worker owns is in `workers.py`.

The run prints nothing itself. It writes events and hands each one to the renderers that watch, so
the terminal, a file and `ducktest5 report` of the same run all say the same thing (`events.py`,
`reports/`).

With `debugger` (`--lldb`) the one selected `.test` case is prepared as usual -- services, batch dir,
`config.json`, init file -- and then handed to the debugger in the foreground instead of being run.
The batch dir stays behind for a second look, or a second debugger.
"""

from __future__ import annotations

import concurrent.futures
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback

from . import caseenv, events, phases, runners, sqllogic
from .binary import UsageError
from .client import SetupFailed
from .ctx import Ctx
from .engines.duckdb import DuckDBSession
from .events import CASE_END, CASE_START, RUN_END, RUN_START, SERVICE
from .mnemonic import run_id as make_run_id
from .model import SQLLOGIC
from .outcomes import ALL as OUTCOMES
from .outcomes import BLOCKED, ERROR, FAILED, PASSED, SKIPPED
from .provision import KEPT, attach_argument
from .reports import Report, render, write_files
from .reports.live import Live
from .workers import Pool


def _describe_failure(exc, body):
    """The error, plus the innermost line of the test's own file: where the body failed, not where a
    helper raised."""
    text = "".join(traceback.format_exception_only(type(exc), exc)).rstrip()
    stack = getattr(exc, "stack", None) or traceback.extract_tb(exc.__traceback__)
    lines = [f.lineno for f in stack if f.filename == body.__code__.co_filename]
    return f"{text}\n  at {body.__code__.co_filename}:{lines[-1]}" if lines else text


def _failure_parts(problems, checks):
    """The first problem, as the parts a report lays out separately. A check that compared rows
    carries them; anything else is a message with the place it happened."""
    if not problems:
        return {}
    out = {"summary": problems[0]}
    if where := events.where_in(problems[0]):
        out["where"] = where
        out["summary"] = "\n".join(line for line in problems[0].splitlines() if line.strip() != f"at {where}").rstrip()
    if checks:
        for name in ("sql", "expected", "actual"):
            if value := getattr(checks[0], name, None):
                out[name] = value
    return out


def _run_body(case, ctx):
    """Returns (outcome, [problem descriptions], the first problem in parts). Checks that failed
    under on_fail="continue" are all reported; an abort or a crash is reported after them; teardowns
    run whatever happened, Ctrl-C too."""
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
    return outcome, problems, _failure_parts(problems, ctx.failed_checks)


def _made_of(case_env, *, batch_dir=None, case_dir=None):
    """What a failed case was made of, for a reader who did not write it: its case environment, with
    where every part came from, and the files that hold the rest. The init SQL is named by its
    source and its file, never quoted: it may hold a secret."""
    facts = {"environment": case_env.record()}
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


def _only_one_test_case(cases):
    """A debugger takes over the terminal, so there is one process to take over."""
    if len(cases) != 1:
        listed = [c.id for c in cases[:10]] + ([f"... and {len(cases) - 10} more"] if len(cases) > 10 else [])
        raise UsageError(
            f"a debugger needs exactly one .test case; the selection gives {len(cases)}:\n  " + "\n  ".join(listed)
        )
    if cases[0].test.type != SQLLOGIC:
        raise UsageError(f"a debugger runs the unittest binary; {cases[0].id} is a Python body -- not supported yet")


def _stop_on_signal(restore=None):
    """SIGTERM kills the binaries this run started and ends the run its own way, so that the event
    file, the report and what was kept are there and `exec --resume` can take it up again. With
    `restore` the handler the run found is put back."""
    if restore is not None:
        if restore["was"] is not None:
            signal.signal(signal.SIGTERM, restore["was"])
        return restore
    sqllogic.start_running()
    stopped = {"by": None, "was": None}

    def handler(signum, frame):
        stopped["by"] = signal.Signals(signum).name
        sqllogic.kill_running()
        raise KeyboardInterrupt(stopped["by"])

    try:
        stopped["was"] = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread: the caller keeps its own handlers
        pass
    return stopped


def _batch_dir(run_dir, batch_id):
    """Where a batch writes. A second exec over one plan (a resume, a re-run of what failed) does
    not write over what the first left: it takes the next free name."""
    base = os.path.join(run_dir, "batches", batch_id)
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-again{n}"):
        n += 1
    return f"{base}-again{n}"


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
    runner=phases.UNITTEST_RUNNER,
    run_args="",
    run_bin=None,
    out=None,
    session_factory=DuckDBSession,
    debugger=None,
    command=None,
    selection=None,
    format="text",
    level="normal",
    color=None,
    report_json=None,
    junit_file=None,
    duckdb_file=None,
):
    """Every phase, in one process and one run directory."""
    if debugger is not None:
        _only_one_test_case(cases)
    run_dir = os.path.join(os.path.abspath(temp_root), make_run_id())
    os.makedirs(run_dir)
    phases.write_tests(run_dir, cases, project, working_dir=working_dir, selection=selection)
    plan = phases.build_plan(cases, batch_size=batch_size, workers=workers, runner=runner, run_args=run_args, keep=keep)
    phases.write_plan(run_dir, plan)
    return execute(
        cases,
        project,
        plan=plan,
        run_dir=run_dir,
        duckdb_bin=duckdb_bin,
        unittest_bin=unittest_bin,
        working_dir=working_dir,
        keep=keep,
        existing_services=existing_services,
        keep_services=keep_services,
        provision_here=True,
        clean=True,
        run_bin=run_bin,
        out=out,
        session_factory=session_factory,
        debugger=debugger,
        command=command,
        selection=selection,
        format=format,
        level=level,
        color=color,
        report_json=report_json,
        junit_file=junit_file,
        duckdb_file=duckdb_file,
    )


def execute(
    cases,
    project,
    *,
    plan,
    run_dir,
    duckdb_bin,
    unittest_bin=None,
    working_dir=".",
    keep=False,
    existing_services=(),
    keep_services=False,
    provision_here=True,
    clean=False,
    run_bin=None,
    events_file=None,
    append=False,
    interrupted=(),
    out=None,
    session_factory=DuckDBSession,
    debugger=None,
    command=None,
    selection=None,
    format="text",
    level="normal",
    color=None,
    report_json=None,
    junit_file=None,
    duckdb_file=None,
):
    """The exec phase: the batches of `plan`, run, as events in `04-events.jsonl`.

    `cases` holds the Case of every id the plan names. With `provision_here` the services are
    started and released here, as `run` does; without it they are attached to, from `03` or from
    `--existing-service`. `interrupted` names cases a run before this one left half-run, so that
    their event says so."""
    out = out or sys.stdout  # looked up now: a caller may have replaced it since import
    by_id = {c.id: c for c in cases}
    if missing := [i for i in phases.ids_of_plan(plan) if i not in by_id]:
        raise UsageError(
            f"{len(missing)} case(s) of the plan are not in the test tree any more (was it changed since `plan`?):\n  "
            + "\n  ".join(missing[:10])
        )
    cases = [by_id[i] for i in phases.ids_of_plan(plan)]
    run_id = phases.run_id_of(run_dir)
    engines = project.engines
    knobs = plan.get("knobs") or {}
    workers = knobs.get("workers", 4)
    runner = knobs.get("runner", phases.UNITTEST_RUNNER)

    verbose = level == "verbose"
    # Text at normal or verbose is the live renderer; anything else is rendered once, at the end.
    live = Live(out, color=color, total=len(cases), level=level) if format == "text" and level != "terse" else None
    writer = events.Writer(
        events_file or os.path.join(run_dir, events.EVENTS_FILE),
        run_id,
        listeners=[live] if live else [],
        append=append,
    )
    started = time.perf_counter()
    writer.emit(
        RUN_START,
        at=events.now(),
        command=events.redact_command(command or sys.argv),
        run_dir=run_dir,
        project_root=project.root,
        working_dir=os.path.abspath(working_dir),
        selection=selection,
        versions=events.versions(duckdb_bin),
        binaries={"duckdb": duckdb_bin, "unittest": unittest_bin},
        cases=len(cases),
    )

    def begin(case, runner_name):
        writer.emit(CASE_START, at=round(time.perf_counter() - started, 2), runner=runner_name, **_identity(case))

    def done(
        case,
        outcome,
        *,
        runner,
        seconds=None,
        problems=(),
        kept=(),
        note="",
        batch_dir=None,
        case_dir=None,
        dirs=(),
        failure=None,
        statements=(),
        reason=None,
        **more,
    ):
        line = {**_identity(case), "outcome": outcome, "runner": runner}
        if seconds is not None:
            line["seconds"] = seconds
        line.update({k: v for k, v in more.items() if v not in (None, (), {})})
        if case.id in interrupted:
            note = "; ".join(x for x in ("the run before this one was stopped while this case was running", note) if x)
        if problems:
            line["problems"] = list(problems)
        if failure:
            line["failure"] = failure
        if reason:
            line["reason"] = reason
        if note:
            line["note"] = note
        if dirs:  # where the cleanup phase finds what this case left
            line["dirs"] = [os.path.relpath(d, run_dir) for d in dirs]
        if outcome != PASSED or verbose:
            line["made_of"] = _made_of(case_env(case), batch_dir=batch_dir, case_dir=case_dir)
            line["again"] = f"ducktest5 run --id {shlex.quote(case.id)}"
        if kept:
            line["kept"] = list(kept)
        if statements and (outcome != PASSED or verbose):
            line["statements"] = list(statements)
        writer.emit(CASE_END, **line)

    provisioned, boot_errors, handles, debugged = {}, {}, {}, False
    resolved = {}

    def case_env(case):
        """The case's environment, computed once: after provisioning, before anything runs."""
        if id(case) not in resolved:
            resolved[id(case)] = caseenv.resolve(case, provisioned)
        return resolved[id(case)]

    # A Python case's timeout, counted from its own begin event (`case_started`), is watched by the
    # main thread, not the worker running it -- see `python_case` and the exec loop below. `reported`
    # makes sure a case that is given up on and a body released late by that never both write an
    # event for it.
    case_started, reported, reported_lock = {}, set(), threading.Lock()

    def report_once(case_id):
        with reported_lock:
            if case_id in reported:
                return False
            reported.add(case_id)
            return True

    def service_event(service_id, *, kind="service", **fields):
        writer.emit(SERVICE, id=service_id, kind=kind, **fields)

    duckdb_home = os.path.join(run_dir, "home")
    pool = Pool(workers, make_session=lambda: session_factory(duckdb_bin, home=duckdb_home), handles=handles)
    stopped = _stop_on_signal()
    blocked = lambda c: [x for x in (*c.services, *c.engines) if x in boot_errors]  # noqa: E731
    code = 1
    try:
        # provision: each service some selected case needs, then each engine. One that does not come
        # up blocks its cases; the rest of the run goes on.
        needed = {s for c in cases for s in c.services}
        if provision_here:
            provisioned, boot_errors = phases.provision_services(
                needed,
                project,
                existing_services=existing_services,
                keep_services=keep_services,
                on=service_event,
            )
            phases.write_provision(run_dir, provisioned, boot_errors, keep_services=keep_services, force=True)
        else:
            provisioned, boot_errors = phases.services_for_exec(
                needed,
                project,
                doc=phases.read_provision(run_dir),
                existing_services=existing_services,
                on=service_event,
            )

        # An engine boots only when a case that can still run needs it.
        for engine_id in sorted({e for c in cases if not blocked(c) for e in c.engines}):
            log = os.path.join(run_dir, f"engine-{engine_id}.log")
            version = engines[engine_id].version
            service_event(engine_id, kind="engine", state="starting", version=version, log=log)
            t0 = time.perf_counter()
            try:
                handles[engine_id] = engines[engine_id].boot(log)
                for service_id in engines[engine_id].uses:
                    if service_id in provisioned:
                        handles[engine_id].use_service(service_id, provisioned[service_id].block)
                service_event(
                    engine_id,
                    kind="engine",
                    state="up",
                    version=version,
                    log=log,
                    seconds=round(time.perf_counter() - t0, 2),
                )
            except Exception as exc:
                boot_errors[engine_id] = f"{type(exc).__name__}: {exc} (log: {log})"
                service_event(engine_id, kind="engine", state="failed", version=version, error=boot_errors[engine_id])

        runnable = {}
        for case in cases:
            if by := blocked(case):
                begin(case, "-")
                done(case, BLOCKED, runner="-", blocked_by=by, note=f"{by[0]} did not come up")
            else:
                runnable[case.id] = case

        # .test bodies with no Python: batches through the unittest binary, several at a time, or
        # through DuckDB's own `run` wrapper.
        batches = []
        for entry in plan.get("batches") or []:
            chunk = [runnable[i] for i in entry["cases"] if i in runnable]
            if not chunk or entry["runner"] == phases.PYTHON_RUNNER:
                continue
            if unittest_bin is None:
                raise UsageError("the selection holds .test files, and no unittest binary was found")
            batches.append((entry, chunk, _batch_of(run_dir, entry, chunk, case_env)))

        if debugger is not None and batches:
            ((_, _, batch),) = batches
            batch.write()
            args = shlex.split(debugger) + sqllogic.batch_args(unittest_bin, batch, working_dir)
            print("\n" + " ".join(shlex.quote(a) for a in args) + "\n", file=out, flush=True)
            debugged = True
            code = subprocess.call(args, cwd=working_dir)  # the terminal is the debugger's
            print(f"\nbatch kept: {batch.dir}", file=out, flush=True)
            if batch.case_env.init_sql:
                print(
                    f"  {sqllogic.INIT_FILE} there may hold a secret; remove the directory when you are done", file=out
                )
            return code

        def start_and_run(chunk, batch):
            """A case starts when its batch does, so a run that is stopped says which cases were
            under way and a resume takes exactly those up again."""
            if sqllogic.stopped():  # a batch still in the queue when the run was stopped never began
                raise RuntimeError("the run was stopped")
            with pool.holding({m for case in chunk for m in case.mutex}):
                for case in chunk:
                    begin(case, runner)
                return sqllogic.run_batch(unittest_bin, batch, working_dir)

        def attempt(case, n, shell, engine_handles):
            """Returns (outcome, problems, the failure in parts, the case's directory, its ctx)."""
            # An attempt after the first has its own directory and prefix, so it does not see what
            # the one before it left.
            dir_name = case.dir_name if n == 1 else f"{case.dir_name}.attempt{n}"
            temp_dir = os.path.join(run_dir, "cases", dir_name)
            try:
                os.makedirs(temp_dir)
                shell.begin_test()
                for handle in engine_handles.values():
                    if hasattr(handle, "begin_test"):
                        handle.begin_test()
                    if hasattr(handle, "begin_case"):  # scopes its jobs, so a timeout can cancel just this case's
                        handle.begin_case(case.id)
                ctx = Ctx(
                    case,
                    temp_dir=temp_dir,
                    duckdb_bin=duckdb_bin,
                    duckdb_session=shell,
                    duckdb_home=duckdb_home,
                    engines=engine_handles,
                    services=provisioned,
                    remote_parts=(run_id, dir_name),
                    case_env=case_env(case),
                    run_test=lambda **kw: _run_paired(case, case_env(case), unittest_bin, working_dir, temp_dir, **kw),
                    session_factory=session_factory,
                )
            except Exception as exc:
                problems = [f"the case could not be set up: {type(exc).__name__}: {exc}"]
                return ERROR, problems, {"summary": problems[0]}, temp_dir, None
            return *_run_body(case, ctx), temp_dir, ctx

        def python_case(case):
            """One Python body, in the worker that took the slot: its own warm shell, its own
            session of every engine, and the locks its mutexes name held for as long as it runs.
            Registered under `pool.running` while it does, so the exec loop below can reach it once
            its timeout passes; `report_once` then keeps this from also reporting it, however late it
            is released."""
            if sqllogic.stopped():  # a case still waiting when the run was stopped never began
                return
            with pool.holding(case.mutex):
                begin(case, phases.PYTHON_RUNNER)
                case_started[case.id] = time.perf_counter()
                t0 = time.perf_counter()
                with pool.running(case.id) as (shell, engine_handles):
                    allowed, tried, first, ctx = 1 + case.test.retries, [], None, None
                    for n in range(1, allowed + 1):
                        outcome, problems, failure, temp_dir, ctx = attempt(case, n, shell, engine_handles)
                        tried.append((temp_dir, ctx))
                        if outcome not in (FAILED, ERROR):
                            break
                        first = first or (outcome, problems, failure)
                note = ""
                if outcome in (FAILED, ERROR):
                    outcome, problems, failure = first
                elif n > 1:
                    note = f"passed on attempt {n} of {allowed}"
                kept = []
                if outcome == PASSED and not keep:
                    # What the case wrote on a service is its own; the files it left are the cleanup phase's.
                    for service_id, uri in (x for _, c in tried if c for x in c.remote_locations):
                        try:
                            provisioned[service_id].service.clean(provisioned[service_id].block, uri)
                        except Exception as exc:
                            outcome = ERROR
                            problems.append(
                                f"what the case wrote at {uri} could not be removed: {type(exc).__name__}: {exc}"
                            )
                            failure = {"summary": problems[-1]}
                            kept.append(uri)
                else:
                    kept = [
                        *(temp_dir for temp_dir, _ in tried if os.path.isdir(temp_dir)),
                        *(uri for _, c in tried if c for _, uri in c.remote_locations),
                    ]
                if sqllogic.stopped():  # stopped while this body ran: a resume takes the case up again
                    return
                if not report_once(case.id):  # its timeout already wrote an ERROR for it
                    return
                done(
                    case,
                    outcome,
                    runner=phases.PYTHON_RUNNER,
                    seconds=round(time.perf_counter() - t0, 2),
                    problems=problems,
                    failure=failure if outcome != PASSED else None,
                    kept=kept,
                    note=note,
                    case_dir=tried[0][0],
                    dirs=[d for d, _ in tried],
                    seconds_by_client=ctx.seconds_by_client() if ctx else {},
                    statements=[s for _, c in tried if c for s in c.statements],
                    **({"attempts": n} if n > 1 else {}),
                )

        if runner == phases.RUN_RUNNER and batches:
            for _, chunk, batch in batches:
                for case in chunk:
                    begin(case, runner)
            for chunk, group, by_case in runners.run_wrapper(
                unittest_bin,
                [(chunk, batch) for _, chunk, batch in batches],
                working_dir,
                run_dir=run_dir,
                workers=workers,
                run_args=knobs.get("run_args", ""),
                run_bin=run_bin,
            ):
                _report_batch(chunk, group, by_case, done, run_dir, runner)
            batches = []

        def python_timed_out(case, case_timeout):
            """A thread cannot be killed: give up waiting on it, release whatever it holds, and
            report it now, so the pool's slot is free for the next case in the plan."""
            pool.cancel(case.id)
            if report_once(case.id):
                done(case, ERROR, runner=phases.PYTHON_RUNNER, note=f"timed out after {case_timeout:.0f}s")

        # One budget of slots for both kinds of body: a batch of `.test` files and a Python case
        # each take one, in plan order. A `.test` batch enforces its own timeout inside the binary and
        # always reports itself here; a Python body reports itself from its worker, unless its
        # timeout -- watched here, since a worker cannot watch its own clock -- gets there first.
        prepared = {entry["id"]: (chunk, batch) for entry, chunk, batch in batches}
        batch_futures, python_deadlines, every_future = {}, {}, []
        for entry in plan.get("batches") or []:
            if entry["id"] in prepared:
                chunk, batch = prepared[entry["id"]]
                future = pool.submit(start_and_run, chunk, batch)
                batch_futures[future] = (chunk, batch)
                every_future.append(future)
            elif entry["runner"] == phases.PYTHON_RUNNER:
                case_timeout = entry.get("timeout") or sqllogic.DEFAULT_TIMEOUT_S
                for i in entry["cases"]:
                    if i not in runnable:
                        continue
                    future = pool.submit(python_case, runnable[i])
                    python_deadlines[future] = (runnable[i], case_timeout)
                    every_future.append(future)

        pending = set(every_future)
        while pending:
            finished, pending = concurrent.futures.wait(
                pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                if future not in batch_futures:
                    future.result()  # a Python case reports its own outcome; what is left is a bug here
                    continue
                chunk, batch = batch_futures[future]
                try:
                    by_case = future.result()
                except Exception as exc:  # the binary could not be started, the batch dir not written, ...
                    for case in chunk:
                        done(
                            case,
                            ERROR,
                            runner=runner,
                            problems=[f"the batch could not run: {type(exc).__name__}: {exc}"],
                            batch_dir=batch.dir,
                        )
                    continue
                _report_batch(chunk, batch, by_case, done, run_dir, runner)
            now = time.perf_counter()
            for future in [f for f in pending if f in python_deadlines]:
                case, case_timeout = python_deadlines[future]
                began = case_started.get(case.id)
                if began is not None and now - began >= case_timeout:
                    pending.discard(future)
                    python_timed_out(case, case_timeout)

    except KeyboardInterrupt:  # Ctrl-C, or the signal handler above
        stopped["by"] = stopped["by"] or "interrupt"
        sqllogic.kill_running()
    finally:
        _stop_on_signal(restore=stopped)
        # The order is what a stopped run needs: no case that was waiting starts, then everything a
        # body can be waiting for is gone, and only then are the workers waited out.
        pool.stop()
        for handle in handles.values():
            try:
                handle.stop()
            except Exception:
                pass
        pool.join()
        if provision_here:
            phases.release_all(
                provisioned,
                on_error=lambda p, exc: service_event(p.service.id, state="stop_failed", error=str(exc)),
            )
            for p in provisioned.values():
                if p.mode == KEPT:
                    service_event(p.service.id, state="kept", attach=attach_argument(p))
        if not debugged:  # the debugger ran the case, so nothing here has an outcome to report
            ended = [e for e in writer.events if e.get("event") == CASE_END]
            counts = {o: sum(e["outcome"] == o for e in ended) for o in OUTCOMES}
            # Green means it ran: a skip inside the binary does not fail the run, anything else not
            # passed does, and a run in which nothing passed is not green.
            bad = counts[FAILED] + counts[ERROR] + counts[BLOCKED]
            code = 0 if bad == 0 and counts[PASSED] > 0 else 1
            if stopped["by"]:
                code = 130
            writer.emit(
                RUN_END,
                at=events.now(),
                counts=counts,
                cases=len(ended),
                seconds=round(time.perf_counter() - started, 2),
                exit_code=code,
                stopped=stopped["by"],
            )
        writer.close()
        if not debugged:
            if clean:
                phases.cleanup(run_dir, project=project, keep=keep, existing_services=existing_services)
            sources = events.files_in(run_dir)
            if writer.path not in sources:  # an event file beside another run's, as a slice writes
                sources.append(writer.path)
            report = Report.of(events.load(sources))
            if live:
                live.finish(report)
            else:
                render(report, out, format=format, level=level, color=bool(color))
            write_files(
                report,
                sources,
                run_dir=run_dir,
                report_json=report_json,
                junit_file=junit_file,
                duckdb=duckdb_file,
                duckdb_bin=duckdb_bin,
            )
    return code


def _batch_of(run_dir, entry, chunk, case_env):
    """One plan batch as the two files the binary takes. The batch key is worked out again here,
    with the services of this process: a plan that puts two environments in one batch is refused,
    not run."""
    keys = {case_env(c).key() for c in chunk}
    if len(keys) > 1:
        raise UsageError(
            f"batch {entry['id']} of the plan holds {len(keys)} case environments; a batch is one environment. "
            "Make the plan again with `ducktest5 plan --force`."
        )
    return sqllogic.Batch(
        _batch_dir(run_dir, entry["id"]),
        [(c.id, c.test.body_file) for c in chunk],
        case_env(chunk[0]),
        entry.get("timeout") or sqllogic.DEFAULT_TIMEOUT_S,
        timeouts={i: t for i, t in (entry.get("timeouts") or {}).items()},
        retries={i: n for i, n in (entry.get("retries") or {}).items()},
    )


def _report_batch(chunk, batch, by_case, done, run_dir, runner):
    """One event for each case of a batch the binary or the wrapper has run."""
    for case in chunk:
        r = by_case.get(case.id)
        if r is None:
            done(
                case,
                ERROR,
                runner=runner,
                problems=[f"the {runner} runner reported nothing for this case"],
                batch_dir=batch.dir,
                batch=os.path.relpath(batch.dir, run_dir),
            )
            continue
        problems = [r.detail] if r.detail and r.outcome != PASSED else []
        done(
            case,
            r.outcome,
            runner=runner,
            seconds=r.seconds,
            problems=problems,
            failure=events.binary_failure(r.detail) if r.outcome in (FAILED, ERROR) else None,
            reason=r.detail if r.outcome == SKIPPED else None,
            kept=[r.kept] if r.kept else [],
            note=r.note,
            batch_dir=batch.dir,
            batch=os.path.relpath(batch.dir, run_dir),
            evidence=r.evidence,
            **({"attempts": r.attempts} if r.attempts > 1 else {}),
        )


def _identity(case):
    """The same fields on `case_start` and `case_end`, so one is enough to know what a line is about."""
    return {
        "id": case.id,
        "test": f"{case.test.root_path}::{case.test.name}",
        "type": case.test.type,
        "file": case.test.path,
        "case": case.case_id,
        "variants": {a: v.id for a, v in case.axis_values.items()},
        "tags": case.tags,
    }


def _run_paired(case, case_env, unittest_bin, working_dir, temp_dir, *, env=None, init_sql=""):
    """`ctx.run_test()`: the `.test` file beside a Python body, as a batch of one."""
    if unittest_bin is None:
        raise UsageError(f"{case.id} runs {case.test.body_file}, and no unittest binary was found")
    batch = sqllogic.Batch(
        os.path.join(temp_dir, "run_test"),
        [(case.id, case.test.body_file)],
        case_env.plus(values=env, init_sql=init_sql),
        case.test.timeout or sqllogic.DEFAULT_TIMEOUT_S,
    )
    result = sqllogic.run_batch(unittest_bin, batch, working_dir)[case.id]
    if result.outcome == SKIPPED:
        raise SetupFailed(f"{case.test.body_file} was skipped inside the binary: {result.detail}")
    if result.outcome != PASSED:
        raise AssertionError(f"{case.test.body_file}\n{result.detail}")
