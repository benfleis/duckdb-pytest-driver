"""`ducktest5 scan|select|run [PATH ...] [--select CLAUSE ...] [--skip CLAUSE ...] [--id CASE_ID ...] [--suite NAME ...]`

`scan --env` prints what each selected case would be given -- the extensions, the statements, the
values, the roots, the process environment, the services -- with the source of every part.
"""

import argparse
import os
import sys

import yaml

from . import caseenv, sqllogic
from .binary import UsageError, find_duckdb
from .run import run
from .scan import scan
from .select import Selection, apply


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ducktest5")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name, text in (
        ("scan", "print every selected case with what it needs"),
        ("select", "print which cases would run, and for each other case why not"),
        ("run", "run the selected cases"),
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
        p.add_argument("--slow", action="store_true", help="also take cases with the tag slow or coverage")
        p.add_argument("--root", default=None, help="where the tests are (default: ./test, else .)")
        if name == "select":
            p.add_argument("--why", action="store_true", help="also print which rule gave each case what")
        if name == "scan":
            p.add_argument(
                "--env",
                action="store_true",
                help="print what each case is given, and where each part came from, instead of the scan record",
            )
    run_options = commands["run"]
    run_options.add_argument(
        "--build", default="debug", help="build/<BUILD>/ holds duckdb and test/unittest (default: debug)"
    )
    run_options.add_argument("--duckdb-bin")
    run_options.add_argument("--unittest-bin")
    run_options.add_argument("--temp-dir-root", default="duckdb_unittest_tempdir")
    run_options.add_argument("--keep", action="store_true", help="keep what passed cases wrote, too")
    run_options.add_argument("--batch-size", type=int, default=10, help=".test files for one unittest process")
    run_options.add_argument("--workers", type=int, default=4, help="unittest processes at a time")
    run_options.add_argument(
        "--no-binary-check", action="store_true", help="do not compare the .test files on disk with `unittest -l`"
    )
    run_options.add_argument(
        "--existing-service",
        action="append",
        default=[],
        metavar="ID[=URL|={json}]",
        help="use a running instance of a service; it is never stopped (also DUCKTEST_EXISTING_SERVICE_<ID>)",
    )
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
    args = parser.parse_args(argv)
    debugger = None
    if getattr(args, "lldb", False) or getattr(args, "debugger", None):
        debugger = args.debugger or os.environ.get("DUCKTEST_DEBUGGER") or "lldb --"

    try:
        cwd = os.getcwd()
        root = args.root or ("test" if os.path.isdir("test") else ".")
        cases, project = scan(root, working_dir=cwd)
        selection = Selection(
            paths=args.paths, select=args.select, ids=args.id, suites=args.suite, slow=args.slow, skip=args.skip
        )
        selected, why_not = apply(cases, selection, project, working_dir=cwd)
        if args.command == "scan":
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
                if args.why:
                    for line in c.test.why:
                        print(f"        {line}")
            for c in cases:
                if c.id in why_not:
                    print(f"skip  {c.id}  -- {why_not[c.id]}")
            print(f"\n{len(selected)} of {len(cases)} cases")
            return 0
        if not selected:
            raise UsageError("no case selected")
        duckdb_bin = find_duckdb(explicit=args.duckdb_bin, build=args.build, working_dir=cwd)
        unittest_bin = None
        if any(c.test.body_file for c in selected):
            unittest_bin = sqllogic.find_unittest(explicit=args.unittest_bin, build=args.build, working_dir=cwd)
            if not args.no_binary_check:
                scratch = os.path.join(args.temp_dir_root, ".list")
                problems = sqllogic.check_against_binary(
                    [c.test.body_file for c in cases if c.test.body_file],
                    unittest_bin,
                    cwd,
                    scratch,
                    project.root,
                    excluded=project.excluded_bodies(cwd),
                )
                if problems:
                    raise UsageError(
                        "the tests on disk and the unittest binary do not agree:\n  " + "\n  ".join(problems)
                    )
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
            debugger=debugger,
        )
    except UsageError as exc:
        print(f"ducktest5: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
