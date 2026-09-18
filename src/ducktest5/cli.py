"""`ducktest5 scan|run PATH [--select TEXT]`"""

import argparse
import os
import sys

import yaml

from .binary import UsageError, find_duckdb
from .run import run
from .scan import scan


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ducktest5")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "run"):
        p = sub.add_parser(name)
        p.add_argument("path")
        p.add_argument("--select", help="keep tests whose id contains this text")
    p.add_argument("--build", default="debug", help="build/<BUILD>/duckdb (default: debug)")
    p.add_argument("--duckdb-bin")
    p.add_argument("--temp-dir-root", default="duckdb_unittest_tempdir")
    p.add_argument("--keep", action="store_true", help="keep temp dirs of passed tests too")
    p.add_argument(
        "--existing-service",
        action="append",
        default=[],
        metavar="ID[=URL|={json}]",
        help="use a running instance of a service; it is never stopped (also DUCKTEST_EXISTING_SERVICE_<ID>)",
    )
    p.add_argument("--keep-services", action="store_true", help="find each service or start it, and leave it running")
    args = parser.parse_args(argv)

    try:
        items, project = scan(args.path, select=args.select, working_dir=os.getcwd())
        engines = project.engines
        if args.command == "scan":
            yaml.safe_dump([i.record(engines) for i in items], sys.stdout, sort_keys=False, width=100)
            return 0
        if not items:
            raise UsageError(f"no tests selected under {args.path}")
        duckdb_bin = find_duckdb(explicit=args.duckdb_bin, build=args.build, working_dir=os.getcwd())
        return run(
            items,
            project,
            duckdb_bin=duckdb_bin,
            temp_root=args.temp_dir_root,
            keep=args.keep,
            existing_services=args.existing_service,
            keep_services=args.keep_services,
        )
    except UsageError as exc:
        print(f"ducktest5: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
