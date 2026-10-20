"""exec: the plan's batches, run, as one event for each test; and `run`, which is every phase.

`ducktest run` is `scan`, `plan`, `provision`, `exec`, `cleanup` and `report` in one process, and
each of them is the function the subcommand of the same name calls (`phases.py`). What was decided
and what happened is in the run dir: `01-tests.yaml`, `02-plan.yaml`, `03-provision.yaml`,
`04-events.jsonl` (one line for each test), `05-report.yaml` and `05-report.json`. Whatever goes
wrong outside a body is an ERROR of the tests it hit, never the end of the run, and services are
released on every way out.

`.test` batches go to the unittest binary, several processes at a time; Python bodies run in
worker threads of this process. Both
kinds take their slot from one budget of `--workers`, and what each worker owns is in `workers.py`.

The run prints nothing itself. It writes events and hands each one to the renderers that watch, so
the terminal, a file and `ducktest report` of the same run all say the same thing (`events.py`,
`reports/`).

With `debugger` (`--lldb`) the one selected test of a `.test` file is prepared as usual -- services, batch dir,
`config.json`, init file -- and then handed to the debugger in the foreground instead of being run.
The batch dir stays behind for a second look, or a second debugger.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback

from . import events, logconfig, phases, leases, sqllogic, testenv
from .binary import UsageError
from .resources.client import SetupFailed
from .ctx import Context
from .engines.duckdb import DuckDBShell
from .events import RUN_END, RUN_START, SERVICE, TEST_END, TEST_START
from .mnemonic import run_id as make_run_id
from .model import SQLLOGIC
from .outcomes import ALL as OUTCOMES
from .outcomes import BLOCKED, ERROR, FAILED, PASSED, SKIPPED, Skip
from .provision import KEPT, adopt, attach_argument, bring_up
from .resources import RO, BootedEngineDef, ServiceNotUp
from .resources.credential import engine_env
from .resources.engine import Runtime
from .resources.made import Made, services_in
from .reports import Report, render, write_files
from .reports.live import Live
from .workers import Pool

_log = logging.getLogger(__name__)


def _describe_failure(exc, test):
    """The error, plus the innermost line of the test's own file: where the body failed, not where a
    helper raised. The file is named as the test id names it."""
    body = test.definition.body
    text = "".join(traceback.format_exception_only(type(exc), exc)).rstrip()
    stack = getattr(exc, "stack", None) or traceback.extract_tb(exc.__traceback__)
    lines = [f.lineno for f in stack if f.filename == body.__code__.co_filename]
    return f"{text}\n  at {test.definition.root_path}:{lines[-1]}" if lines else text


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


def _run_body(test, ctx):
    """Returns (outcome, [problem descriptions], the first problem in parts). Checks that failed
    under on_fail="continue" are all reported; an abort or a crash is reported after them; teardowns
    run whatever happened, Ctrl-C too. A skip is SKIPPED with its reason as the one problem, unless a
    check had already failed."""
    body, outcome, problems = test.definition.body, PASSED, []
    try:
        try:
            body(ctx)
        except Skip as exc:
            outcome, problems = SKIPPED, [exc.reason]
        except UsageError as exc:
            outcome, problems = (
                ERROR,
                [f"usage error of the test, not of the command line\n{_describe_failure(exc, test)}"],
            )
        except SetupFailed as exc:
            outcome, problems = ERROR, [f"setup failed\n{_describe_failure(exc, test)}"]
        except Exception as exc:
            outcome, problems = FAILED, [_describe_failure(exc, test)]
        if ctx.failed_checks:
            problems = [_describe_failure(f, test) for f in ctx.failed_checks] + (
                [] if outcome == SKIPPED else problems
            )
            outcome = FAILED  # also over a setup that failed later: the check had already proven a failure
    finally:
        for teardown in reversed(ctx.teardowns):
            try:
                teardown()
            except Exception as exc:
                problems.append(f"teardown failed\n{exc}")
                outcome = ERROR if outcome in (PASSED, SKIPPED) else outcome
    failure = {} if outcome == SKIPPED else _failure_parts(problems, ctx.failed_checks)
    return outcome, problems, failure


def _made_of(test_env, *, batch_dir=None, test_dir=None):
    """What a failed test was made of, for a reader who did not write it: its test environment, with
    where every part came from, and the files that hold the rest. The init SQL is named by its
    source and its file, never quoted: it may hold a secret."""
    facts = {"environment": test_env.record()}
    files = {}
    if batch_dir:
        files = {
            "config": os.path.join(batch_dir, "config.json"),
            "tests": os.path.join(batch_dir, "tests.txt"),
            "init": os.path.join(batch_dir, sqllogic.INIT_FILE),
            "output": os.path.join(batch_dir, "output.txt"),
        }
    if test_dir:
        files = {"dir": test_dir}
    files = {name: path for name, path in files.items() if os.path.exists(path)}
    if files:
        facts["files"] = files
    return facts


def _only_one_test(tests):
    """A debugger takes over the terminal, so there is one process to take over."""
    if len(tests) != 1:
        listed = [c.name for c in tests[:10]] + ([f"... and {len(tests) - 10} more"] if len(tests) > 10 else [])
        raise UsageError(
            f"a debugger needs exactly one test of a .test file; the selection gives {len(tests)}:\n  "
            + "\n  ".join(listed)
        )
    if tests[0].definition.type != SQLLOGIC:
        raise UsageError(f"a debugger runs the unittest binary; {tests[0].name} is a Python body -- not supported yet")


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
    tests,
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
    shell_factory=DuckDBShell,
    debugger=None,
    command=None,
    selection=None,
    format="text",
    level="normal",
    color=None,
    report_json=None,
    junit_file=None,
):
    """Every phase, in one process and one run directory."""
    if debugger is not None:
        _only_one_test(tests)
    run_dir = os.path.join(os.path.abspath(temp_root), make_run_id())
    os.makedirs(run_dir)
    logconfig.add_run_dir(run_dir)
    phases.write_tests(run_dir, tests, project, working_dir=working_dir, selection=selection)
    plan = phases.build_plan(tests, project, batch_size=batch_size, workers=workers, keep=keep)
    phases.write_plan(run_dir, plan)
    return execute(
        tests,
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
        out=out,
        shell_factory=shell_factory,
        debugger=debugger,
        command=command,
        selection=selection,
        format=format,
        level=level,
        color=color,
        report_json=report_json,
        junit_file=junit_file,
    )


def execute(
    tests,
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
    events_file=None,
    append=False,
    interrupted=(),
    out=None,
    shell_factory=DuckDBShell,
    debugger=None,
    command=None,
    selection=None,
    format="text",
    level="normal",
    color=None,
    report_json=None,
    junit_file=None,
):
    """The exec phase: the batches of `plan`, run, as events in `04-events.jsonl`.

    `tests` holds the Test of every id the plan names. With `provision_here` the services are
    made and ended here, as `run` does; without it they are adopted where `03` or
    `--existing-service` says they answer. `interrupted` names tests a run before this one left half-run, so that
    their event says so."""
    out = out or sys.stdout  # looked up now: a caller may have replaced it since import
    by_id = {c.name: c for c in tests}
    if missing := [i for i in phases.ids_of_plan(plan) if i not in by_id]:
        raise UsageError(
            f"{len(missing)} test(s) of the plan are not in the test tree any more (was it changed since `plan`?):\n  "
            + "\n  ".join(missing[:10])
        )
    tests = [by_id[i] for i in phases.ids_of_plan(plan)]
    run_id = phases.run_id_of(run_dir)
    engines = project.engines
    knobs = plan.get("knobs") or {}
    workers = knobs.get("workers", 4)
    runner = phases.UNITTEST_RUNNER

    verbose = level == "verbose"
    # Text at normal or verbose is the live renderer; anything else is rendered once, at the end.
    live = (
        Live(out, color=color, total=len(tests), level=level, workers=workers)
        if format == "text" and level != "terse"
        else None
    )
    writer = events.Writer(
        events_file or os.path.join(run_dir, events.EVENTS_FILE),
        run_id,
        listeners=[live] if live else [],
        append=append,
    )
    started = time.perf_counter()
    _log.info("run start: %s (%d tests) -> %s", run_id, len(tests), run_dir)
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
        tests=len(tests),
    )

    again_flags = _carried(command or [])

    def begin(test, runner_name, *, worker=None):
        writer.emit(
            TEST_START, at=round(time.perf_counter() - started, 2), runner=runner_name, worker=worker, **_identity(test)
        )

    def done(
        test,
        outcome,
        *,
        runner,
        seconds=None,
        problems=(),
        kept=(),
        note="",
        batch_dir=None,
        test_dir=None,
        dirs=(),
        failure=None,
        statements=(),
        reason=None,
        **more,
    ):
        line = {**_identity(test), "outcome": outcome, "runner": runner}
        if seconds is not None:
            line["seconds"] = seconds
        line.update({k: v for k, v in more.items() if v not in (None, (), {})})
        if test.name in interrupted:
            note = "; ".join(x for x in ("the run before this one was stopped while this test was running", note) if x)
        if problems:
            line["problems"] = list(problems)
        if failure:
            line["failure"] = failure
        if reason:
            line["reason"] = reason
        if note:
            line["note"] = note
        if dirs:  # where the cleanup phase finds what this test left
            line["dirs"] = [os.path.relpath(d, run_dir) for d in dirs]
        if outcome != PASSED or verbose:
            line["made_of"] = _made_of(test_env(test), batch_dir=batch_dir, test_dir=test_dir)
            line["again"] = shlex.join(["ducktest", "run", "--id", test.name, *again_flags])
        if kept:
            line["kept"] = list(kept)
        if statements and (outcome != PASSED or verbose):
            line["statements"] = list(statements)
        writer.emit(TEST_END, **line)

    boot_errors, handles, debugged = {}, {}, False
    made = Made(run_id)
    resolved = {}
    # The run directory is the default storage's run level, wherever --run-dir put it.
    default = project.default_storage.with_(temp_root=os.path.dirname(os.path.abspath(run_dir)))

    def test_env(test, key=None):
        """The test's environment, computed once: after provisioning, before anything runs. A `.test`
        body gets it under its DuckDB engine, with the run's temp roots; a Python body's engines are
        bound to it as it is, with its own place for `key` under the run level of its temp root."""
        if test.definition.type == SQLLOGIC:
            key = None
        elif key is None:
            key = test.dir_name
        if (id(test), key) not in resolved:
            env = testenv.resolve(test, made, key=key, default=default)
            resolved[(id(test), key)] = (
                leases.environment(test, env, project) if test.definition.type == SQLLOGIC else env
            )
        return resolved[(id(test), key)]

    # A Python test's timeout, counted from its own begin event (`test_started`), is watched by the
    # main thread, not the worker running it -- see `python_test` and the exec loop below. `reported`
    # makes sure a test that is given up on and a body released late by that never both write an
    # event for it.
    test_started, reported, reported_lock = {}, set(), threading.Lock()

    def report_once(test_id):
        with reported_lock:
            if test_id in reported:
                return False
            reported.add(test_id)
            return True

    def service_event(service_id, *, kind="service", **fields):
        writer.emit(SERVICE, name=service_id, kind=kind, **fields)

    duckdb_home = os.path.join(run_dir, "home")

    runtime = Runtime(shell=lambda env: shell_factory(duckdb_bin, home=duckdb_home, env=env), booted=handles)
    pool = Pool(workers, runtime=runtime, made=made)
    stopped = _stop_on_signal()
    unresolved = {}  # credential id -> why none of its sources resolved

    refusals = {}  # "<service id>/<part>" -> why the service refused that part

    def refused(c):
        """A part of a service that is up, which the service refused and the test needs (a bucket)."""
        inputs = [d for d, _ in c.inputs]
        out = []
        for d in services_in(inputs):
            service = made.services.get(d.name)
            for part in d.parts_needed(inputs) if service is not None and service.refused else ():
                if part in service.refused:
                    refusals[f"{d.name}/{part}"] = service.refused[part]
                    out.append(f"{d.name}/{part}")
        return out

    def blocked(c):
        down = [x for x in (*c.services, *c.engines) if x in boot_errors]
        down += refused(c)
        return down + [f"credential {d.name}" for d, _ in c.inputs if d.kind == "credential" and d.name in unresolved]

    code = 1
    try:
        # provision: each service some selected test needs, then each engine. One that does not come
        # up blocks its tests; the rest of the run goes on.
        needed = phases.services_of(tests)
        if provision_here:
            boot_errors = bring_up(
                made, needed, existing_services=existing_services, keep=keep_services, on=service_event
            )
            phases.write_provision(run_dir, made, boot_errors, force=True)
        else:
            recorded = phases.read_provision(run_dir).get("services") or {}
            boot_errors = adopt(made, needed, recorded, existing_services=existing_services, on=service_event)
            phases.write_provision(run_dir, made, boot_errors, recorded=recorded, force=True)

        # A credential none of whose sources resolve blocks its tests, as a service that is not up does.
        for test in tests:
            for d, mode in test.inputs:
                if d.kind == "credential" and d.name not in unresolved and not blocked(test):
                    try:
                        credential = made.make(d, mode)
                        credential.start()
                        credential.release(credential.acquire(RO), RO)
                    except UsageError as exc:
                        unresolved[d.name] = str(exc)
                    except ServiceNotUp:
                        pass  # the service blocks it

        # An engine booted for the run boots only when a test that can still run needs it.
        needed_engines = {e for c in tests if not blocked(c) for e in c.engines}
        for engine_id in sorted(e for e in needed_engines if isinstance(engines[e], BootedEngineDef)):
            log = os.path.join(run_dir, f"engine-{engine_id}.log")
            version = engines[engine_id].version
            service_event(engine_id, kind="engine", state="starting", version=version, log=log)
            _log.info("engine %s: starting (booted, version %s)", engine_id, version)
            t0 = time.perf_counter()
            try:
                with _boot_environ(_credential_env(tests, engine_id, made)):
                    handles[engine_id] = engines[engine_id].boot(log)
                service_event(
                    engine_id,
                    kind="engine",
                    state="up",
                    version=version,
                    log=log,
                    seconds=round(time.perf_counter() - t0, 2),
                )
                _log.info("engine %s: up (version %s)", engine_id, version)
            except Exception as exc:
                boot_errors[engine_id] = f"{type(exc).__name__}: {exc} (log: {log})"
                service_event(engine_id, kind="engine", state="failed", version=version, error=boot_errors[engine_id])
                _log.info("engine %s: failed to start: %s", engine_id, boot_errors[engine_id])

        runnable = {}
        for test in tests:
            if by := blocked(test):
                begin(test, "-")
                why = (
                    refusals.get(by[0])
                    or unresolved.get(by[0].removeprefix("credential "))
                    or f"{by[0]} did not come up"
                )
                done(test, BLOCKED, runner="-", blocked_by=by, note=why)
            else:
                runnable[test.name] = test

        # .test bodies with no Python: batches through the unittest binary, several at a time.
        batches = []
        for entry in plan.get("batches") or []:
            chunk = [runnable[i] for i in entry["tests"] if i in runnable]
            if not chunk or entry["runner"] == phases.PYTHON_RUNNER:
                continue
            if unittest_bin is None:
                raise UsageError("the selection holds .test files, and no unittest binary was found")
            batches.append((entry, chunk, _batch_of(run_dir, entry, chunk, test_env)))

        if debugger is not None and batches:
            ((_, _, batch),) = batches
            batch.write()
            args = shlex.split(debugger) + sqllogic.batch_args(unittest_bin, batch, working_dir)
            print("\n" + " ".join(shlex.quote(a) for a in args) + "\n", file=out, flush=True)
            debugged = True
            code = subprocess.call(
                args, cwd=working_dir, env=batch.test_env.environ()
            )  # the terminal is the debugger's
            print(f"\nbatch kept: {batch.dir}", file=out, flush=True)
            if batch.test_env.init_sql:
                print(
                    f"  {sqllogic.INIT_FILE} there may hold a secret; remove the directory when you are done", file=out
                )
            return code

        def start_and_run(chunk, batch):
            """A test starts when its batch does, so a run that is stopped says which tests were
            under way and a resume takes exactly those up again. All of a batch's tests run as one
            unittest process on the worker that took the slot, so one worker number covers them all."""
            if sqllogic.stopped():  # a batch still in the queue when the run was stopped never began
                raise RuntimeError("the run was stopped")
            worker = getattr(threading.current_thread(), "worker", None)
            with pool.holding({m for test in chunk for m in test.mutex}):
                for test in chunk:
                    begin(test, runner, worker=worker)
                _log.info("batch %s: starting, %d test(s)", os.path.basename(batch.dir), len(chunk))
                results = sqllogic.run_batch(unittest_bin, batch, working_dir)
                _log.info("batch %s: finished, %d test(s)", os.path.basename(batch.dir), len(chunk))
                return worker, results

        def attempt(test, n, lease):
            """Returns (outcome, problems, the failure in parts, the test's directory, its ctx)."""
            # An attempt after the first has its own directory, so it does not see what
            # the one before it left.
            dir_name = test.dir_name if n == 1 else f"{test.dir_name}.attempt{n}"
            temp_dir = default.test_dir(run_id, dir_name)
            held = {}
            try:
                os.makedirs(temp_dir)
                env = test_env(test, dir_name)
                held = pool.take(lease, leases.bind(test, env, project))
                for engine in held.values():
                    if hasattr(engine.live, "scope_jobs"):  # so a timeout can cancel just this test's jobs
                        engine.live.scope_jobs(test.name)
                ctx = Context(
                    test,
                    env,
                    held.values(),
                    temp_dir=temp_dir,
                    services={s: made.services[s] for s in test.services},
                    run_paired=lambda **kw: _run_paired(
                        test,
                        leases.environment(test, env, project),
                        unittest_bin,
                        working_dir,
                        temp_dir,
                        **kw,
                    ),
                )
            except Exception as exc:
                pool.give_back(lease, held, failed=True)
                problems = [f"the test could not be set up: {type(exc).__name__}: {exc}"]
                return ERROR, problems, {"summary": problems[0]}, temp_dir, None
            outcome, problems, failure = _run_body(test, ctx)
            pool.give_back(lease, held, failed=outcome in (FAILED, ERROR))
            return outcome, problems, failure, temp_dir, ctx

        def place_of(test, dir_name):
            """The test's own place under the storage `temp=` binds, which stays with its temp dir when it failed."""
            try:
                return test_env(test, dir_name).root_paths.get("temp_dir_root")
            except Exception:  # its environment could not be made: the attempt reported that already
                return None

        def python_test(test):
            """One Python body, in the worker that took the slot: the worker's engine instances, and
            the locks its mutexes name held for as long as it runs. Its engine instances are registered
            under its id while it does, so the exec loop below can reach them once its timeout passes; `report_once` then keeps this from also reporting it, however late it
            is released."""
            if sqllogic.stopped():  # a test still waiting when the run was stopped never began
                return
            worker = getattr(threading.current_thread(), "worker", None)
            with pool.lease(test.name) as lease, pool.holding(test.mutex, lease):
                begin(test, phases.PYTHON_RUNNER, worker=worker)
                _log.info("test %s: starting", test.name)
                test_started[test.name] = time.perf_counter()
                t0 = time.perf_counter()
                allowed, tried, first, ctx = 1 + test.retries, [], None, None
                for n in range(1, allowed + 1):
                    outcome, problems, failure, temp_dir, ctx = attempt(test, n, lease)
                    if lease.revoked:  # its timeout reported it and released what it held
                        return
                    tried.append((temp_dir, ctx))
                    if outcome not in (FAILED, ERROR):
                        break
                    first = first or (outcome, problems, failure)
                note = ""
                if outcome in (FAILED, ERROR):
                    outcome, problems, failure = first
                elif n > 1:
                    note = f"passed on attempt {n} of {allowed}"
                if n > 1:
                    _log.warning("test %s: retried (%d of %d attempts), ended %s", test.name, n, allowed, outcome)
                kept = []
                if outcome not in (PASSED, SKIPPED) or keep:
                    places = [place_of(test, os.path.basename(d)) for d, _ in tried]
                    found = dict.fromkeys([d for d, _ in tried] + places)
                    kept = [p for p in found if p and ("://" in p or os.path.isdir(p))]
                if sqllogic.stopped():  # stopped while this body ran: a resume takes the test up again
                    return
                if not report_once(test.name):  # its timeout already wrote an ERROR for it
                    return
                _log.info("test %s: finished, %s", test.name, outcome)
                done(
                    test,
                    outcome,
                    runner=phases.PYTHON_RUNNER,
                    seconds=round(time.perf_counter() - t0, 2),
                    problems=problems,
                    failure=failure if outcome not in (PASSED, SKIPPED) else None,
                    reason=problems[0] if outcome == SKIPPED else None,
                    kept=kept,
                    note=note,
                    test_dir=tried[0][0],
                    dirs=[d for d, _ in tried],
                    seconds_by_client=ctx.seconds_by_client() if ctx else {},
                    statements=[s for _, c in tried if c for s in c.statements],
                    worker=worker,
                    **({"attempts": n} if n > 1 else {}),
                )

        def python_timed_out(test, test_timeout):
            """A thread cannot be killed: give up waiting on it, release whatever it holds, and
            report it now; a new thread takes its slot for the next test in the plan."""
            worker = pool.worker_of(test.name)
            # Claimed before the revoke: the body then fails on its dead engine, and must not report that.
            first = report_once(test.name)
            pool.revoke(test.name)
            if first:
                done(
                    test, ERROR, runner=phases.PYTHON_RUNNER, note=f"timed out after {test_timeout:.0f}s", worker=worker
                )

        # One budget of slots for both kinds of body: a batch of `.test` files and a Python test
        # each take one, in plan order. A `.test` batch enforces its own timeout inside the binary and
        # always reports itself here; a Python body reports itself from its worker, unless its
        # timeout -- watched here, since a worker cannot watch its own clock -- gets there first.
        prepared = {entry["name"]: (chunk, batch) for entry, chunk, batch in batches}
        batch_futures, python_deadlines, every_future = {}, {}, []
        for entry in plan.get("batches") or []:
            if entry["name"] in prepared:
                chunk, batch = prepared[entry["name"]]
                future = pool.submit(start_and_run, chunk, batch)
                batch_futures[future] = (chunk, batch)
                every_future.append(future)
            elif entry["runner"] == phases.PYTHON_RUNNER:
                test_timeout = entry.get("timeout") or sqllogic.DEFAULT_TIMEOUT_S
                for i in entry["tests"]:
                    if i not in runnable:
                        continue
                    future = pool.submit(python_test, runnable[i], owner=i)
                    python_deadlines[future] = (runnable[i], test_timeout)
                    every_future.append(future)

        pending = set(every_future)
        while pending:
            finished, pending = concurrent.futures.wait(
                pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                if future not in batch_futures:
                    future.result()  # a Python test reports its own outcome; what is left is a bug here
                    continue
                chunk, batch = batch_futures[future]
                try:
                    worker, by_test = future.result()
                except Exception as exc:  # the binary could not be started, the batch dir not written, ...
                    for test in chunk:
                        done(
                            test,
                            ERROR,
                            runner=runner,
                            problems=[f"the batch could not run: {type(exc).__name__}: {exc}"],
                            batch_dir=batch.dir,
                        )
                    continue
                _report_batch(chunk, batch, by_test, done, run_dir, runner, worker=worker)
            now = time.perf_counter()
            for future in [f for f in pending if f in python_deadlines]:
                test, test_timeout = python_deadlines[future]
                began = test_started.get(test.name)
                if began is not None and now - began >= test_timeout:
                    pending.discard(future)
                    python_timed_out(test, test_timeout)

    except KeyboardInterrupt:  # Ctrl-C, or the signal handler above
        stopped["by"] = stopped["by"] or "interrupt"
        sqllogic.kill_running()
    finally:
        _stop_on_signal(restore=stopped)
        # The order is what a stopped run needs: no test that was waiting starts, then everything a
        # body can be waiting for is gone, and only then are the workers waited out.
        pool.stop()
        for engine_id, handle in handles.items():
            _log.info("engine %s: stopping", engine_id)
            try:
                handle.stop()
            except Exception:
                pass
        pool.join()
        failed = any(e.get("outcome") in (FAILED, ERROR) for e in writer.events if e.get("event") == TEST_END)
        for service_id, service in made.services.items():
            if made.modes[service_id] == KEPT:
                service_event(service_id, state="kept", attach=attach_argument(service))
                _log.info("service %s (kept): left running at %s", service_id, service.address)

        def _stop_failed(service, exc):
            service_event(service.name, state="stop_failed", error=str(exc))
            _log.warning("service %s: could not be stopped: %s", service.name, exc)

        made.end(failed, on_error=_stop_failed)
        if not debugged:  # the debugger ran the test, so nothing here has an outcome to report
            ended = [e for e in writer.events if e.get("event") == TEST_END]
            counts = {o: sum(e["outcome"] == o for e in ended) for o in OUTCOMES}
            # Green means it ran: a skip does not fail the run, anything else not
            # passed does, and a run in which nothing passed is not green.
            bad = counts[FAILED] + counts[ERROR] + counts[BLOCKED]
            code = 0 if bad == 0 and counts[PASSED] > 0 else 1
            # Before `run_end`, so that it holds what cleanup could not remove and the code says so.
            left = phases.cleanup(run_dir, project=project, keep=keep, ended=True)["problems"] if clean else []
            for problem in left:
                _log.info("cleanup: %s", problem)  # not a warning: it goes to stderr after the report
            if left:
                code = max(code, 1)
            if stopped["by"]:
                code = 130
            writer.emit(
                RUN_END,
                at=events.now(),
                counts=counts,
                tests=len(ended),
                seconds=round(time.perf_counter() - started, 2),
                exit_code=code,
                stopped=stopped["by"],
                cleanup=left or None,
            )
            _log.info("run end: %s, exit %d, %s -> %s", run_id, code, counts, run_dir)
        writer.close()
        if not debugged:
            sources = events.files_in(run_dir)
            if writer.path not in sources:  # an event file beside another run's, as a slice writes
                sources.append(writer.path)
            report = Report.of(events.load(sources))
            if live:
                live.finish(report)
            else:
                render(report, out, format=format, level=level, color=bool(color))
            write_files(report, run_dir=run_dir, report_json=report_json, junit_file=junit_file)
            for problem in left:
                print(problem, file=sys.stderr)
    return code


# What a re-run of one test needs to find the same tests: option -> how many
# values follow. Binary paths stay out: they are of one machine, and `--build`/$BUILD_DIR find them.
_CARRIED = {"--build": 1, "--root": 1, "--data-dir": 1}


def _carried(command):
    """The options of `command` that a re-run of one of its tests must be given again."""
    out, args = [], list(command[1:])
    while args:
        arg = args.pop(0)
        name = arg.split("=", 1)[0]
        if name in _CARRIED:
            out.append(arg)
            if "=" not in arg:
                out += args[: _CARRIED[name]]
                del args[: _CARRIED[name]]
    return out


def _credential_env(tests, engine_id, made):
    """The SDK env of the credentials the tests on a booted engine hold: it boots once, so it gets
    them when it boots. Scan made sure there is one of each type. One that does not resolve is left
    out; its tests say why."""
    env, seen = {}, set()
    for test in tests:
        if engine_id not in test.engines:
            continue
        for d, mode in test.inputs:
            if d.kind != "credential" or d.name in seen:
                continue
            seen.add(d.name)
            try:
                credential = made.make(d, mode)
                credential.start()
                credential.release(credential.acquire(RO), RO)
            except (UsageError, ServiceNotUp):
                continue
            env.update({name: str(a.get()) for name, a in credential.secrets.items()})
    return env


@contextlib.contextmanager
def _boot_environ(add):
    """What a boot starts (a JVM) takes this process's env: for the boot, it is the env any engine
    starts from (`engine_env`), with `add`; the run's own after."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({**engine_env(saved), **add})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _batch_of(run_dir, entry, chunk, test_env):
    """One plan batch as the two files the binary takes. The batch key, the identity of its tests, is
    worked out again here, with the services of this process: a plan that puts two
    environments in one batch is refused, not run."""
    keys = {leases.batch_identity(test_env(c)) for c in chunk}
    if len(keys) > 1:
        raise UsageError(
            f"batch {entry['name']} of the plan holds {len(keys)} test environments; a batch is one environment. "
            "Make the plan again with `ducktest plan --force`."
        )
    return sqllogic.Batch(
        _batch_dir(run_dir, entry["name"]),
        [(c.name, c.definition.body_file) for c in chunk],
        test_env(chunk[0]),
        entry.get("timeout") or sqllogic.DEFAULT_TIMEOUT_S,
        timeouts={i: t for i, t in (entry.get("timeouts") or {}).items()},
        retries={i: n for i, n in (entry.get("retries") or {}).items()},
    )


def _report_batch(chunk, batch, by_test, done, run_dir, runner, *, worker=None):
    """One event for each test of a batch the binary has run. All of a batch's tests ran as one
    unittest process on `worker`."""
    for test in chunk:
        r = by_test.get(test.name)
        if r is None:
            done(
                test,
                ERROR,
                runner=runner,
                problems=[f"the {runner} runner reported nothing for this test"],
                batch_dir=batch.dir,
                batch=os.path.relpath(batch.dir, run_dir),
                worker=worker,
            )
            continue
        problems = [r.detail] if r.detail and r.outcome != PASSED else []
        done(
            test,
            r.outcome,
            runner=runner,
            seconds=r.seconds,
            problems=problems,
            failure=_binary_failure(test, r.detail) if r.outcome in (FAILED, ERROR) else None,
            reason=r.detail if r.outcome == SKIPPED else None,
            kept=[r.kept] if r.kept else [],
            note=r.note,
            batch_dir=batch.dir,
            batch=os.path.relpath(batch.dir, run_dir),
            worker=worker,
            **({"attempts": r.attempts} if r.attempts > 1 else {}),
        )


def _binary_failure(test, detail):
    """The binary names the file as it was given, relative to the working directory; a reader
    finds it by the test id, relative to the project root."""
    failure = events.binary_failure(detail)
    file, _, line = failure.get("where", "").rpartition(":")
    if file == test.definition.body_file:
        failure["where"] = f"{test.definition.root_path}:{line}"
    return failure


def _identity(test):
    """The same fields on `test_start` and `test_end`, so one is enough to know what a line is about."""
    return {
        "name": test.name,
        "test": f"{test.definition.root_path}::{test.definition.name}",
        "type": test.definition.type,
        "file": test.definition.path,
        "matrix": {p: v.name for p, v in test.cell.items()},
        "tags": test.tags,
    }


def _run_paired(test, test_env, unittest_bin, working_dir, temp_dir, *, values=None, init_sql=""):
    """`ctx.run_paired()`: the `.test` file beside a Python body, as a batch of one."""
    if unittest_bin is None:
        raise UsageError(f"{test.name} runs {test.definition.body_file}, and no unittest binary was found")
    batch = sqllogic.Batch(
        os.path.join(temp_dir, "run_paired"),
        [(test.name, test.definition.body_file)],
        test_env.plus(values=values, init_sql=init_sql),
        test.timeout or sqllogic.DEFAULT_TIMEOUT_S,
    )
    result = sqllogic.run_batch(unittest_bin, batch, working_dir)[test.name]
    if result.outcome == SKIPPED:
        raise Skip(result.detail)
    if result.outcome != PASSED:
        raise AssertionError(f"{test.definition.body_file}\n{result.detail}")
