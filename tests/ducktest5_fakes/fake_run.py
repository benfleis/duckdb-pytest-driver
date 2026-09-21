#!/usr/bin/env python3
"""A stand-in for DuckDB's `test/run` wrapper, for ducktest5's self-tests. It speaks the same
contract as the real one, in the part ducktest uses:

    --test-list FILE    the tests to run, one per line
    --test-flags '...'  passed to the binary as they are
    --test-command T    the template a batch is run with: {binary} {flags} {test_list}
    --workers N         batches at a time
    --batch-size N      tests in one batch (default 10)
    --retry N           run a failed batch again, up to N times

Like the real one it forwards the output of a **failing** batch only, prints a summary line, and
ends 0 when every test passed and 1 when one did not. The binary it runs is its own sibling, as in
a build directory.
"""

import argparse
import os
import shlex
import subprocess
import sys
import tempfile

BINARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_unittest.py")


def run_batch(args, names):
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as f:
        f.write("".join(n + "\n" for n in names))
        list_path = f.name
    command = args.test_command.format(
        binary=shlex.quote(BINARY),
        flags=shlex.join(shlex.split(args.test_flags)),
        test_list=shlex.quote(list_path),
    )
    done = subprocess.run(shlex.split(command), capture_output=True, text=True)
    os.unlink(list_path)
    return done


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-list", required=True)
    parser.add_argument("--test-flags", default="")
    parser.add_argument("--test-command", default="{binary} {flags} -f {test_list}")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--retry", type=int, default=0)
    args = parser.parse_args(argv)
    os.chdir(tempfile.gettempdir())  # the real one runs from the DuckDB source root, not from the caller's directory

    names = [line.strip() for line in open(args.test_list) if line.strip()]
    batches = [names[i : i + args.batch_size] for i in range(0, len(names), args.batch_size)]
    failed = 0
    for batch in batches:
        for attempt in range(1 + args.retry):
            done = run_batch(args, batch)
            if done.returncode == 0:
                break
        if done.returncode != 0:
            failed += 1
            print(f"error: FAIL {batch[0]}")
            print(done.stdout, end="")  # the real one forwards a failing batch, and only that
            print(done.stderr, end="", file=sys.stderr)
    if failed:
        print(f"{failed} of {len(batches)} batches failed")
        return 1
    print(f"all tests passed in 0s ({len(names)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
