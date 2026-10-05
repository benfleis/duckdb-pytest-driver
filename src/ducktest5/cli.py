"""`ducktest5 scan|select|run [PATH ...] [--select CLAUSE ...] [--skip CLAUSE ...] [--id TEST_ID ...] [--suite NAME ...]`

`scan --env` prints what each selected test would be given -- the extensions, the statements, the
values, the roots, the process environment, the inputs -- with the source of every part.

The phases are subcommands over one run directory, each reading the files of the phases before it
and writing its own (`phases.py`):

    ducktest5 scan      PATH ... --run-dir DIR     -> 01-tests.yaml
    ducktest5 plan      --run-dir DIR              -> 02-plan.yaml
    ducktest5 provision --run-dir DIR              -> 03-provision.yaml
    ducktest5 exec      --run-dir DIR              -> 04-events.jsonl
    ducktest5 cleanup   --run-dir DIR              removes what passed, keeps what failed
    ducktest5 report    --run-dir DIR              prints it, in any format

`ducktest5 run` is all of them in one process. A phase that finds its own output already there
refuses unless `--force`; `exec` takes `--resume` (the tests with no end yet), `--only-failed`,
`--batch` and `--id` instead, so two terminals or two machines can take a part of one plan each and
`report --run-dir A --run-dir B` puts the two event files together.

`ducktest5 report --run-dir DIR [DIR ...]` prints an old run again. Nothing is recomputed: every
format reads `04-events.jsonl` and only that. Several run dirs merge into one report, a later one
winning a test id they share, so "what does the whole matrix say" is one command.

`ducktest5 init` writes the repo's test environment, `pyproject.toml`, and locks it (`init.py`).
"""

import argparse
import os
import re
import sys

import yaml

from . import events, init, logconfig, phases, leases, sqllogic, testenv
from .binary import UsageError, check_floor, find_duckdb
from .conf import load_project, repo_root
from .declare import CONF_FILE
from .provision import bring_up
from .resources.made import Made
from .resources.storage import DEFAULT_TEMP_ROOT
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
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")


def _binary_options(parser):
    """Which build runs the tests. The binaries are of a machine, so they are named here and are
    never written into a plan."""
    parser.add_argument(
        "--build",
        default="debug",
        help="build/<BUILD>/ holds duckdb and test/unittest, looked for at the top of the git repository, then "
        "in the working directory; $BUILD_DIR names the directory instead (default: debug)",
    )
    parser.add_argument("--duckdb-bin", help="the duckdb shell to use (default: found by --build)")
    parser.add_argument("--unittest-bin", help="the test/unittest binary to use (default: found by --build)")


def _storage_options(parser, *, temp=True):
    """The default storage of the run: what a test that binds no storage is given. A storage a
    test binds keeps its own roots."""
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        help="DATA_DIR of a test that binds no storage (default: data/ at the top of the git repository, "
        "else of the project root)",
    )
    if temp:
        parser.add_argument(
            "--temp-dir-root",
            metavar="DIR",
            help=f"where run directories go; a run's is the TEMP_DIR_ROOT of a test that binds no storage "
            f"(default: ./{DEFAULT_TEMP_ROOT})",
        )


def _diagnostics_options(parser):
    """What the tool logs about itself, apart from the report on stdout (`--format`, `--level`)."""
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="diagnostics on stderr: once for INFO, twice for DEBUG (always DEBUG in <run dir>/ducktest.log)",
    )
    parser.add_argument("--log-file", metavar="FILE", help="also write DEBUG diagnostics there")


def _service_options(parser):
    parser.add_argument(
        "--existing-service",
        action="append",
        default=[],
        metavar="ID=URL",
        help="adopt the instance of a service that answers at URL; it is never stopped (also DUCKTEST_EXISTING_SERVICE_<ID>=URL)",
    )


def _plan_options(parser):
    parser.add_argument("--batch-size", type=int, default=10, help=".test files for one unittest process")
    parser.add_argument(
        "--workers", type=int, default=4, help="how many run at a time: a .test batch or a Python test takes one slot"
    )


def _color(choice, out):
    return wants_colour(out) if choice == "auto" else choice == "always"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ducktest5")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name, text in (
        ("scan", "print every selected test with what it needs, or write 01-tests.yaml"),
        ("select", "print which tests would run, and how many were left out and why"),
        ("run", "run the selected tests: every phase, in one process"),
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
            help="same CLAUSE syntax as --select; removes a test from the base; repeats OR",
        )
        p.add_argument(
            "--id",
            action="append",
            default=[],
            metavar="TEST_ID",
            help="a test id, verbatim (its path is relative to the project root); it runs whatever else is given",
        )
        p.add_argument(
            "--suite", action="append", default=[], metavar="NAME", help="a suite of the project; repeats union"
        )
        p.add_argument("--root", default=None, help="where the tests are (default: ./test, else .)")
        if name == "select":
            p.add_argument(
                "--explain",
                action="store_true",
                help="also print which rule gave each test what, and why each other test is left out",
            )
        if name == "scan":
            p.add_argument(
                "--env",
                action="store_true",
                help="print what each test is given, and where each part came from, instead of the scan record",
            )
            p.add_argument("--run-dir", metavar="DIR", help="write 01-tests.yaml there instead of printing the record")
            p.add_argument("--force", action="store_true", help="write 01-tests.yaml again if it is already there")
            _storage_options(p)
    run_options = commands["run"]
    _binary_options(run_options)
    _storage_options(run_options)
    run_options.add_argument("--keep", action="store_true", help="keep what passed tests wrote, too")
    _plan_options(run_options)
    _service_options(run_options)
    run_options.add_argument(
        "--keep-services", action="store_true", help="leave each service it starts running, with what it holds"
    )
    run_options.add_argument(
        "--lldb",
        action="store_true",
        help="prepare the one selected test of a .test file as usual, then start a debugger on the binary instead of running it",
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
    _storage_options(exec_options, temp=False)
    exec_options.add_argument("--keep", action="store_true", help="keep what passed tests wrote, too")
    exec_options.add_argument(
        "--provision", action="store_true", help="provision the services here, as `run` does, instead of reading 03"
    )
    exec_options.add_argument(
        "--resume", action="store_true", help="run only the tests of the plan that have no end yet"
    )
    exec_options.add_argument(
        "--only-failed", action="store_true", help="run again the tests whose last outcome was failed, error or blocked"
    )
    exec_options.add_argument(
        "--batch", action="append", default=[], metavar="ID", help="only this batch of the plan; repeats union"
    )
    exec_options.add_argument(
        "--id", action="append", default=[], metavar="TEST_ID", help="only this test of the plan; repeats union"
    )
    exec_options.add_argument(
        "--events",
        metavar="FILE",
        help="write the events there instead of 04-events.jsonl, so that two execs over one plan do not write over each other",
    )
    _service_options(exec_options)
    _report_options(exec_options)
    provision_options = sub.add_parser("provision", help="bring up the services the plan needs and write 03")
    _service_options(provision_options)
    provision_options.add_argument(
        "--keep-services", action="store_true", help="leave each service running after cleanup, too"
    )
    cleanup_options = sub.add_parser("cleanup", help="remove what passed tests left, keep what failed, stop services")
    cleanup_options.add_argument("--keep", action="store_true", help="keep what passed tests wrote, too")
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

    commands["init"] = init.add_parser(sub)

    report_options = sub.add_parser("report", help="print a run that already happened, in any format")
    report_options.add_argument(
        "--run-dir",
        action="append",
        default=[],
        required=True,
        metavar="DIR",
        help="a run directory or an event file; repeats merge, a later one winning a shared test id",
    )
    _report_options(report_options)

    for p in (*commands.values(), report_options):
        _diagnostics_options(p)

    args = parser.parse_args(argv)
    logconfig.configure(args.verbose)
    if args.log_file:
        logconfig.add_file(args.log_file)
    debugger = None
    if getattr(args, "lldb", False) or getattr(args, "debugger", None):
        debugger = args.debugger or os.environ.get("DUCKTEST_DEBUGGER") or "lldb --"

    try:
        if args.command == "init":
            return init.run(args)
        if args.command == "report":
            return _report(args)
        if args.command in ("plan", "provision", "exec", "cleanup"):
            logconfig.add_run_dir(args.run_dir)
            return {"plan": _plan, "provision": _provision, "exec": _exec, "cleanup": _cleanup}[args.command](args)
        cwd = os.getcwd()
        tests, project = _scan(args, cwd)
        selection = Selection(paths=args.paths, select=args.select, ids=args.id, suites=args.suite, skip=args.skip)
        selected, why_not = apply(tests, selection, project, working_dir=cwd)
        asked = {"paths": args.paths, "select": args.select, "skip": args.skip, "ids": args.id, "suites": args.suite}
        if args.command == "scan":
            if args.run_dir:
                logconfig.add_run_dir(args.run_dir)
                path = phases.write_tests(
                    args.run_dir, selected, project, working_dir=cwd, selection=asked, force=args.force
                )
                print(f"{len(selected)} tests -> {path}")
                return 0
            if args.env:
                if project.pythonpath:
                    paths = ", ".join(os.path.relpath(p, project.root) for p in project.pythonpath)
                    print(f"pythonpath: {paths}  <- {CONF_FILE}")
                for c in selected:
                    print(c.id)
                    # No service is started here, so what a variant makes out of one is named, not called.
                    for line in (
                        leases.lines(c, project)
                        + c.bind_lines()
                        + testenv.resolve(c, default=project.default_storage).lines()
                    ):
                        print(f"  {line}")
                return 0
            yaml.safe_dump([c.record(project) for c in selected], sys.stdout, sort_keys=False, width=100)
            return 0
        if args.command == "select":
            for c in selected:
                print(f"run   {c.id}")
                if args.explain:
                    for line in c.why:
                        print(f"        {line}")
            if args.explain:
                for c in tests:
                    if c.id in why_not:
                        print(f"skip  {c.id}  -- {why_not[c.id]}")
            print(f"\n{_select_summary(selected, tests, why_not, explained=args.explain)}")
            return 0
        if not selected:
            raise UsageError("no test selected")
        duckdb_bin, unittest_bin = _binaries(args, selected, project, cwd)
        return run(
            selected,
            project,
            duckdb_bin=duckdb_bin,
            unittest_bin=unittest_bin,
            working_dir=cwd,
            temp_root=project.default_storage.temp_root,
            keep=args.keep,
            existing_services=args.existing_service,
            keep_services=args.keep_services,
            batch_size=args.batch_size,
            workers=args.workers,
            debugger=debugger,
            command=["ducktest5"] + list(argv if argv is not None else sys.argv[1:]),
            selection=asked,
            format=args.format,
            level=args.level,
            color=_color(args.color, sys.stdout),
            report_json=args.report_json,
            junit_file=args.junit,
        )
    except UsageError as exc:
        print(f"ducktest5: {exc}", file=sys.stderr)
        return 2


def _select_summary(selected, tests, why_not, *, explained):
    """`2 of 9 tests; left out: 6 outside the given paths, 1 not in suite 'default'`: each reason cut
    before the detail that differs per test, so a tree of thousands is one line."""
    line = f"{len(selected)} of {len(tests)} tests"
    if not why_not:
        return line
    groups = {}
    for reason in why_not.values():
        kind = re.split(r":|  <- ", reason, maxsplit=1)[0].strip()
        groups[kind] = groups.get(kind, 0) + 1
    by_count = sorted(groups.items(), key=lambda kv: -kv[1])
    line += "; left out: " + ", ".join(f"{n} {kind}" for kind, n in by_count)
    return line if explained else line + " (--explain lists each)"


def _scan(args, working_dir):
    root = args.root or ("test" if os.path.isdir("test") else ".")
    tests, project = scan(root, working_dir=working_dir)
    project.default_storage = project.default_storage.with_(
        data_dir=getattr(args, "data_dir", None), temp_root=getattr(args, "temp_dir_root", None)
    )
    return tests, project


def _tests_by_id(args, ids, working_dir):
    """(the Tests a phase file names, the project). A file of a phase holds ids, not bodies: a Python
    body is only in the tree, and a test of a `.test` file must find the file of that name."""
    tests, project = _scan(args, working_dir)
    by_id = {c.id: c for c in tests}
    if missing := [i for i in ids if i not in by_id]:
        raise UsageError(
            f"{len(missing)} test(s) of the run directory are not in {project.root} any more:\n  "
            + "\n  ".join(missing[:10])
        )
    return [by_id[i] for i in ids], project


def _binaries(args, selected, project, working_dir):
    """The duckdb shell and, when a `.test` file is selected, the unittest binary of this machine."""
    places = (repo_root(project.root), working_dir)
    duckdb_bin = find_duckdb(explicit=args.duckdb_bin, build=args.build, places=places)
    unittest_bin = None
    if any(c.definition.body_file for c in selected):
        unittest_bin = sqllogic.find_unittest(explicit=args.unittest_bin, build=args.build, places=places)
    scratch = os.path.join(getattr(args, "run_dir", None) or project.default_storage.temp_root, ".version-probe")
    check_floor(duckdb_bin, unittest_bin, scratch)
    return duckdb_bin, unittest_bin


def _plan(args):
    cwd = os.getcwd()
    tests, project = _tests_by_id(args, phases.ids_of_tests(phases.read_tests(args.run_dir)), cwd)
    doc = phases.build_plan(tests, project, batch_size=args.batch_size, workers=args.workers)
    path = phases.write_plan(args.run_dir, doc, force=args.force)
    print(f"{len(doc['batches'])} batches for {len(tests)} tests -> {path}")
    return 0


def _provision(args):
    cwd = os.getcwd()
    tests, project = _tests_by_id(args, phases.ids_of_plan(phases.read_plan(args.run_dir)), cwd)
    made = Made(phases.run_id_of(args.run_dir))
    errors = bring_up(
        made,
        phases.services_of(tests),
        existing_services=args.existing_service,
        keep=args.keep_services,
        separate=True,
    )
    path = phases.write_provision(args.run_dir, made, errors, owned=True, force=args.force)
    for service_id, mode in sorted(made.modes.items()):
        print(f"{service_id}: {mode} at {made.services[service_id].address}")
    for service_id, problem in sorted(errors.items()):
        print(f"{service_id}: {problem}", file=sys.stderr)
    print(f"{len(made.services)} services -> {path}")
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
    test_ids = None
    if args.resume or args.only_failed:
        outcomes = phases.outcomes_of(history)
        ended = {i for i in outcomes if i not in phases.unfinished(history)}
        test_ids = set()
        if args.resume:
            test_ids |= {i for i in phases.ids_of_plan(plan) if i not in ended}
        if args.only_failed:
            test_ids |= {i for i, o in outcomes.items() if o in phases.KEPT_OUTCOMES}
    if args.id:
        test_ids = set(args.id) if test_ids is None else test_ids & set(args.id)
    slice_of = phases.narrow(plan, batch_ids=args.batch, test_ids=test_ids)
    if not slice_of["batches"]:
        print("no test left to run")
        return 0
    tests, project = _tests_by_id(args, phases.ids_of_plan(slice_of), cwd)
    duckdb_bin, unittest_bin = _binaries(args, tests, project, cwd)
    return execute(
        tests,
        project,
        plan=slice_of,
        run_dir=args.run_dir,
        duckdb_bin=duckdb_bin,
        unittest_bin=unittest_bin,
        working_dir=cwd,
        keep=args.keep,
        existing_services=args.existing_service,
        provision_here=args.provision,
        events_file=args.events,
        append=append,
        interrupted=set(phases.unfinished(history)),
        command=["ducktest5"] + sys.argv[1:],
        format=args.format,
        level=args.level,
        color=_color(args.color, sys.stdout),
        report_json=args.report_json,
        junit_file=args.junit,
    )


def _cleanup(args):
    # The project file alone, not a scan: cleanup needs the services, never a body.
    project = load_project(args.root or ("test" if os.path.isdir("test") else "."))
    summary = phases.cleanup(args.run_dir, project=project, keep=args.keep)
    print(f"removed {len(summary['removed'])} directories of tests that passed")
    for path in summary["kept"]:
        print(f"kept: {path}")
    for service_id in summary["stopped"]:
        print(f"stopped: {service_id}")
    for handle in summary["swept"]:
        print(f"removed, left by a run that did not end: {handle}")
    for problem in summary["problems"]:
        print(problem, file=sys.stderr)
    return 1 if summary["problems"] else 0


def _report(args):
    """An old run, read back. Its exit code is the run's own, so re-reporting a red run is red."""
    for path in args.run_dir:
        if not events.files_in(path) or not any(os.path.isfile(p) for p in events.files_in(path)):
            raise UsageError(f"no {events.EVENTS_FILE} in {path}")
    report = Report.of(events.load(args.run_dir))
    render(report, sys.stdout, format=args.format, level=args.level, color=_color(args.color, sys.stdout))
    write_files(report, report_json=args.report_json, junit_file=args.junit)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
