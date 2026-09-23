"""`ducktest5 scan|select|run [PATH ...] [--select CLAUSE ...] [--skip CLAUSE ...] [--id CASE_ID ...] [--suite NAME ...]`

`scan --env` prints what each selected case would be given -- the extensions, the statements, the
values, the roots, the process environment, the services -- with the source of every part.

The phases are subcommands over one run directory, each reading the files of the phases before it
and writing its own (`phases.py`):

    ducktest5 scan      PATH ... --run-dir DIR     -> 01-tests.yaml
    ducktest5 plan      --run-dir DIR              -> 02-plan.yaml
    ducktest5 provision --run-dir DIR              -> 03-provision.yaml
    ducktest5 exec      --run-dir DIR              -> 04-events.jsonl
    ducktest5 cleanup   --run-dir DIR              removes what passed, keeps what failed
    ducktest5 report    --run-dir DIR              prints it, in any format

`ducktest5 run` is all of them in one process. A phase that finds its own output already there
refuses unless `--force`; `exec` takes `--resume` (the cases with no end yet), `--only-failed`,
`--batch` and `--id` instead, so two terminals or two machines can take a part of one plan each and
`report --run-dir A --run-dir B` puts the two event files together.

`ducktest5 report --run-dir DIR [DIR ...]` prints an old run again. Nothing is recomputed: every
format reads `04-events.jsonl` and only that. Several run dirs merge into one report, a later one
winning a case id they share, so "what does the whole matrix say" is one command.
"""

import argparse
import os
import sys

import yaml

from . import caseenv, events, phases, sqllogic
from .binary import UsageError, find_duckdb
from .conf import load_project
from .reports import FORMATS, LEVELS, Report, render, write_files
from .reports.live import wants_colour
from .run import execute, run
from .scan import scan
from .select import Selection, apply


def _report_options(parser):
    """The options that say where a report goes; `run`, `exec` and `report` take the same ones."""
    parser.add_argument("--format", default="text", choices=FORMATS, help="what standard output gets (default: text)")
    parser.add_argument("--level", default="normal", choices=LEVELS, help="how much text says (default: normal)")
    parser.add_argument("--junit", metavar="FILE", help="also write JUnit XML there, for CI annotations")
    parser.add_argument("--report-json", metavar="FILE", help="also write the run as one JSON document there")
    parser.add_argument(
        "--duckdb", metavar="FILE", help="also load the events into that DuckDB file; runs append to it"
    )
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")


def _binary_options(parser):
    """Which build runs the tests. The binaries are of a machine, so they are named here and are
    never written into a plan."""
    parser.add_argument(
        "--build", default="debug", help="build/<BUILD>/ holds duckdb and test/unittest (default: debug)"
    )
    parser.add_argument("--duckdb-bin")
    parser.add_argument("--unittest-bin")
    parser.add_argument("--run-bin", help="DuckDB's run wrapper, for --runner run (default: beside test/unittest)")


def _service_options(parser):
    parser.add_argument(
        "--existing-service",
        action="append",
        default=[],
        metavar="ID[=URL|={json}]",
        help="use a running instance of a service; it is never stopped (also DUCKTEST_EXISTING_SERVICE_<ID>)",
    )


def _plan_options(parser):
    parser.add_argument("--batch-size", type=int, default=10, help=".test files for one unittest process")
    parser.add_argument(
        "--workers", type=int, default=4, help="how much runs at a time: a .test batch or a Python case takes one slot"
    )
    parser.add_argument(
        "--runner",
        default=phases.UNITTEST_RUNNER,
        choices=phases.RUNNERS,
        help="what runs a .test batch (default: unittest); a Python body always runs in this process",
    )
    parser.add_argument(
        "--run-args",
        default="",
        metavar="'...'",
        help="passed to DuckDB's run wrapper as they are, with --runner run (its --retry, --batch-timeout, ...)",
    )


def _color(choice, out):
    return wants_colour(out) if choice == "auto" else choice == "always"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ducktest5")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name, text in (
        ("scan", "print every selected case with what it needs, or write 01-tests.yaml"),
        ("select", "print which cases would run, and for each other case why not"),
        ("run", "run the selected cases: every phase, in one process"),
    ):
        p = commands[name] = sub.add_parser(name, help=text)
        p.add_argument("paths", nargs="*", metavar="PATH", help="files, directories or globs; several are a union")
        p.add_argument(
            "--select",
            action="append",
            default=[],
            metavar="CLAUSE",
            help="key=v1,v2[;key=v...] (';'=and, '!key=v' negates a term); repeats OR",
        )
        p.add_argument(
            "--skip",
            action="append",
            default=[],
            metavar="CLAUSE",
            help="same CLAUSE syntax as --select; removes a case from the base; repeats OR",
        )
        p.add_argument(
            "--id",
            action="append",
            default=[],
            metavar="CASE_ID",
            help="a case id, verbatim (its path is relative to the project root); it runs whatever else is given",
        )
        p.add_argument(
            "--suite", action="append", default=[], metavar="NAME", help="a suite of the project; repeats union"
        )
        p.add_argument("--root", default=None, help="where the tests are (default: ./test, else .)")
        if name == "select":
            p.add_argument("--explain", action="store_true", help="also print which rule gave each case what")
        if name == "scan":
            p.add_argument(
                "--env",
                action="store_true",
                help="print what each case is given, and where each part came from, instead of the scan record",
            )
            p.add_argument("--run-dir", metavar="DIR", help="write 01-tests.yaml there instead of printing the record")
            p.add_argument("--force", action="store_true", help="write 01-tests.yaml again if it is already there")
    run_options = commands["run"]
    _binary_options(run_options)
    run_options.add_argument("--temp-dir-root", default="duckdb_unittest_tempdir")
    run_options.add_argument("--keep", action="store_true", help="keep what passed cases wrote, too")
    _plan_options(run_options)
    run_options.add_argument(
        "--no-binary-check", action="store_true", help="do not compare the .test files on disk with `unittest -l`"
    )
    _service_options(run_options)
    run_options.add_argument(
        "--keep-services", action="store_true", help="find each service or start it, and leave it running"
    )
    run_options.add_argument(
        "--lldb",
        action="store_true",
        help="prepare the one selected .test case as usual, then start a debugger on the binary instead of running it",
    )
    run_options.add_argument(
        "--debugger",
        metavar="CMD",
        help="what to put before the binary, instead of 'lldb --' (also $DUCKTEST_DEBUGGER); implies --lldb",
    )
    _report_options(run_options)

    plan_options = sub.add_parser("plan", help="read 01-tests.yaml and write 02-plan.yaml: the batches")
    _plan_options(plan_options)
    exec_options = sub.add_parser("exec", help="run the batches of 02-plan.yaml and write 04-events.jsonl")
    _binary_options(exec_options)
    exec_options.add_argument("--keep", action="store_true", help="keep what passed cases wrote, too")
    exec_options.add_argument(
        "--no-binary-check", action="store_true", help="do not compare the .test files on disk with `unittest -l`"
    )
    exec_options.add_argument(
        "--provision", action="store_true", help="provision the services here, as `run` does, instead of reading 03"
    )
    exec_options.add_argument(
        "--resume", action="store_true", help="run only the cases of the plan that have no end yet"
    )
    exec_options.add_argument(
        "--only-failed", action="store_true", help="run again the cases whose last outcome was failed, error or blocked"
    )
    exec_options.add_argument(
        "--batch", action="append", default=[], metavar="ID", help="only this batch of the plan; repeats union"
    )
    exec_options.add_argument(
        "--id", action="append", default=[], metavar="CASE_ID", help="only this case of the plan; repeats union"
    )
    exec_options.add_argument(
        "--events",
        metavar="FILE",
        help="write the events there instead of 04-events.jsonl, so that two execs over one plan do not clobber",
    )
    _service_options(exec_options)
    _report_options(exec_options)
    provision_options = sub.add_parser("provision", help="bring up the services the plan needs and write 03")
    _service_options(provision_options)
    provision_options.add_argument(
        "--keep-services", action="store_true", help="leave a service that was already running to the next run, too"
    )
    cleanup_options = sub.add_parser("cleanup", help="remove what passed cases left, keep what failed, stop services")
    cleanup_options.add_argument("--keep", action="store_true", help="keep what passed cases wrote, too")
    _service_options(cleanup_options)
    for name, p in (
        ("plan", plan_options),
        ("provision", provision_options),
        ("exec", exec_options),
        ("cleanup", cleanup_options),
    ):
        commands[name] = p
        p.add_argument("--run-dir", required=True, metavar="DIR", help="the run directory the phases share")
        p.add_argument("--root", default=None, help="where the tests are (default: ./test, else .)")
        if name != "cleanup":
            p.add_argument("--force", action="store_true", help="do this phase again although its file is there")

    report_options = sub.add_parser("report", help="print a run that already happened, in any format")
    report_options.add_argument(
        "--run-dir",
        action="append",
        default=[],
        required=True,
        metavar="DIR",
        help="a run directory or an event file; repeats merge, a later one winning a shared case id",
    )
    report_options.add_argument(
        "--duckdb-bin", help="the duckdb shell for --duckdb (default: the one the run recorded)"
    )
    _report_options(report_options)

    args = parser.parse_args(argv)
    debugger = None
    if getattr(args, "lldb", False) or getattr(args, "debugger", None):
        debugger = args.debugger or os.environ.get("DUCKTEST_DEBUGGER") or "lldb --"

    try:
        if args.command == "report":
            return _report(args)
        if args.command in ("plan", "provision", "exec", "cleanup"):
            return {"plan": _plan, "provision": _provision, "exec": _exec, "cleanup": _cleanup}[args.command](args)
        cwd = os.getcwd()
        cases, project = _scan(args, cwd)
        selection = Selection(paths=args.paths, select=args.select, ids=args.id, suites=args.suite, skip=args.skip)
        selected, why_not = apply(cases, selection, project, working_dir=cwd)
        asked = {"paths": args.paths, "select": args.select, "skip": args.skip, "ids": args.id, "suites": args.suite}
        if args.command == "scan":
            if args.run_dir:
                path = phases.write_tests(
                    args.run_dir, selected, project, working_dir=cwd, selection=asked, force=args.force
                )
                print(f"{len(selected)} cases -> {path}")
                return 0
            if args.env:
                for c in selected:
                    print(c.id)
                    # No service is started here, so what a variant makes out of one is named, not called.
                    for line in caseenv.resolve(c).lines():
                        print(f"  {line}")
                return 0
            yaml.safe_dump([c.record(project) for c in selected], sys.stdout, sort_keys=False, width=100)
            return 0
        if args.command == "select":
            for c in selected:
                print(f"run   {c.id}")
                if args.explain:
                    for line in c.test.why:
                        print(f"        {line}")
            for c in cases:
                if c.id in why_not:
                    print(f"skip  {c.id}  -- {why_not[c.id]}")
            print(f"\n{len(selected)} of {len(cases)} cases")
            return 0
        if not selected:
            raise UsageError("no case selected")
        duckdb_bin, unittest_bin = _binaries(args, selected, cases, project, cwd)
        return run(
            selected,
            project,
            duckdb_bin=duckdb_bin,
            unittest_bin=unittest_bin,
            working_dir=cwd,
            temp_root=args.temp_dir_root,
            keep=args.keep,
            existing_services=args.existing_service,
            keep_services=args.keep_services,
            batch_size=args.batch_size,
            workers=args.workers,
            runner=args.runner,
            run_args=args.run_args,
            run_bin=args.run_bin,
            debugger=debugger,
            command=["ducktest5"] + list(argv if argv is not None else sys.argv[1:]),
            selection=asked,
            format=args.format,
            level=args.level,
            color=_color(args.color, sys.stdout),
            report_json=args.report_json,
            junit_file=args.junit,
            duckdb_file=args.duckdb,
        )
    except UsageError as exc:
        print(f"ducktest5: {exc}", file=sys.stderr)
        return 2


def _scan(args, working_dir):
    root = args.root or ("test" if os.path.isdir("test") else ".")
    return scan(root, working_dir=working_dir)


def _cases_by_id(args, ids, working_dir):
    """(the Cases a phase file names, every case of the tree, the project). A file of a phase holds
    ids, not bodies: a Python body is only in the tree, and a `.test` case must be the file of that
    name. The whole tree comes back too, because the check against the binary is of the tree, not of
    the cases one invocation runs."""
    cases, project = _scan(args, working_dir)
    by_id = {c.id: c for c in cases}
    if missing := [i for i in ids if i not in by_id]:
        raise UsageError(
            f"{len(missing)} case(s) of the run directory are not in {project.root} any more:\n  "
            + "\n  ".join(missing[:10])
        )
    return [by_id[i] for i in ids], cases, project


def _binaries(args, selected, cases, project, working_dir):
    """The duckdb shell and, when a `.test` file is selected, the unittest binary of this machine."""
    duckdb_bin = find_duckdb(explicit=args.duckdb_bin, build=args.build, working_dir=working_dir)
    unittest_bin = None
    if any(c.test.body_file for c in selected):
        unittest_bin = sqllogic.find_unittest(explicit=args.unittest_bin, build=args.build, working_dir=working_dir)
        if not args.no_binary_check:
            scratch = os.path.join(getattr(args, "temp_dir_root", None) or args.run_dir, ".list")
            problems = sqllogic.check_against_binary(
                [c.test.body_file for c in cases if c.test.body_file],
                unittest_bin,
                working_dir,
                scratch,
                project.root,
                excluded=project.excluded_bodies(working_dir),
            )
            if problems:
                raise UsageError("the tests on disk and the unittest binary do not agree:\n  " + "\n  ".join(problems))
    return duckdb_bin, unittest_bin


def _plan(args):
    cwd = os.getcwd()
    cases, _, _ = _cases_by_id(args, phases.ids_of_tests(phases.read_tests(args.run_dir)), cwd)
    doc = phases.build_plan(
        cases, batch_size=args.batch_size, workers=args.workers, runner=args.runner, run_args=args.run_args
    )
    path = phases.write_plan(args.run_dir, doc, force=args.force)
    print(f"{len(doc['batches'])} batches for {len(cases)} cases -> {path}")
    return 0


def _provision(args):
    cwd = os.getcwd()
    cases, _, project = _cases_by_id(args, phases.ids_of_plan(phases.read_plan(args.run_dir)), cwd)
    provisioned, errors = phases.provision_services(
        {s for c in cases for s in c.services},
        project,
        existing_services=args.existing_service,
        keep_services=args.keep_services,
        separate=True,
    )
    path = phases.write_provision(args.run_dir, provisioned, errors, keep_services=args.keep_services, force=args.force)
    for service_id, p in sorted(provisioned.items()):
        print(f"{service_id}: {p.mode}")
    for service_id, problem in sorted(errors.items()):
        print(f"{service_id}: {problem}", file=sys.stderr)
    print(f"{len(provisioned)} services -> {path}")
    return 1 if errors else 0


def _exec(args):
    cwd = os.getcwd()
    plan = phases.read_plan(args.run_dir)
    history = events.read(args.run_dir)
    events_path = args.events or os.path.join(args.run_dir, events.EVENTS_FILE)
    append = os.path.isfile(events_path) and os.path.getsize(events_path) > 0
    if append and not (args.resume or args.only_failed or args.force):
        raise UsageError(
            f"{events_path} is already there: this plan ran. Use --resume for what has no end yet, --only-failed, "
            "--events FILE to write beside it, or --force to run it all again."
        )
    case_ids = None
    if args.resume or args.only_failed:
        outcomes = phases.outcomes_of(history)
        ended = {i for i in outcomes if i not in phases.unfinished(history)}
        case_ids = set()
        if args.resume:
            case_ids |= {i for i in phases.ids_of_plan(plan) if i not in ended}
        if args.only_failed:
            case_ids |= {i for i, o in outcomes.items() if o in phases.KEPT_OUTCOMES}
    if args.id:
        case_ids = set(args.id) if case_ids is None else case_ids & set(args.id)
    slice_of = phases.narrow(plan, batch_ids=args.batch, case_ids=case_ids)
    if not slice_of["batches"]:
        print("no case left to run")
        return 0
    cases, every, project = _cases_by_id(args, phases.ids_of_plan(slice_of), cwd)
    duckdb_bin, unittest_bin = _binaries(args, cases, every, project, cwd)
    return execute(
        cases,
        project,
        plan=slice_of,
        run_dir=args.run_dir,
        duckdb_bin=duckdb_bin,
        unittest_bin=unittest_bin,
        working_dir=cwd,
        keep=args.keep,
        existing_services=args.existing_service,
        provision_here=args.provision,
        run_bin=args.run_bin,
        events_file=args.events,
        append=append,
        interrupted=set(phases.unfinished(history)),
        command=["ducktest5"] + sys.argv[1:],
        format=args.format,
        level=args.level,
        color=_color(args.color, sys.stdout),
        report_json=args.report_json,
        junit_file=args.junit,
        duckdb_file=args.duckdb,
    )


def _cleanup(args):
    # The project file alone, not a scan: cleanup needs the services, never a body.
    project = load_project(args.root or ("test" if os.path.isdir("test") else "."))
    summary = phases.cleanup(args.run_dir, project=project, keep=args.keep, existing_services=args.existing_service)
    print(f"removed {len(summary['removed'])} directories of cases that passed")
    for path in summary["kept"]:
        print(f"kept: {path}")
    for service_id in summary["stopped"]:
        print(f"stopped: {service_id}")
    for problem in summary["problems"]:
        print(problem, file=sys.stderr)
    return 1 if summary["problems"] else 0


def _report(args):
    """An old run, read back. Its exit code is the run's own, so re-reporting a red run is red."""
    for path in args.run_dir:
        if not events.files_in(path) or not any(os.path.isfile(p) for p in events.files_in(path)):
            raise UsageError(f"no {events.EVENTS_FILE} in {path}")
    report = Report.of(events.load(args.run_dir))
    files = [f for p in args.run_dir for f in events.files_in(p)]
    render(report, sys.stdout, format=args.format, level=args.level, color=_color(args.color, sys.stdout))
    write_files(
        report,
        files,
        report_json=args.report_json,
        junit_file=args.junit,
        duckdb=args.duckdb,
        duckdb_bin=args.duckdb_bin,
    )
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
