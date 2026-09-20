#!/usr/bin/env python3
"""A stand-in for DuckDB's unittest binary, for ducktest5's self-tests. It speaks the same contract:
`-f LIST --test-config JSON --emit-test-events`, one begin and one end event for each test, and
`-l PATTERN` lists. What a test does comes from its file name:

    *fail*   ends with status error        *hang*    never ends
    *crash*  exits before its end event    *skip*    ends as skip-requirement
    *giveup* prints a SKIP_TEST line and ends ok, as the binary does for an error on its skip list
    *onlyinbatch_fail* fails beside other tests and passes alone
    *stallatexit* / *badexit* end ok, then the process hangs / exits 7
"""

import json
import sys
import time


def event(**fields):
    print("[TEST_EVENT] " + json.dumps(fields), file=sys.stderr, flush=True)


def main(argv):
    if "-l" in argv:
        print("name\tgroup")
        for line in open(argv[argv.index("--list-from") + 1]) if "--list-from" in argv else []:
            print(f"{line.strip()}\t[x]")
        return 3
    names = [line.strip() for line in open(argv[argv.index("-f") + 1]) if line.strip()]
    config = json.load(open(argv[argv.index("--test-config") + 1]))
    failed = False
    for name in names:
        temp = f"{config['temp_dir_root']}/run/{name.replace('/', '_')}"
        event(event="begin", name=name, temp_dir=temp)
        if "hang" in name:
            time.sleep(60)
        if "crash" in name:
            print("Segmentation fault (fake)", flush=True)
            return 139
        if "env" in name:
            print("ENV " + json.dumps({e["env_name"]: e["env_value"] for e in config["test_env"]}), flush=True)
        if "onlyinbatch" in name and len(names) == 1:
            event(event="end", name=name, status="ok", passes=1, fails=0, **{"skip-mode": 0}, temp_dir=temp)
            continue
        if "fail" in name:
            print(f"Wrong result in query! ({name}:4)\nExpected result:\n2\nActual result:\n1", flush=True)
            event(event="end", name=name, status="error", passes=0, fails=1, **{"skip-mode": 0}, temp_dir=temp)
            failed = True
        elif "giveup" in name:
            print(f"[SKIP_TEST] {name} :: skip on error_message matching 'HTTP'", file=sys.stderr, flush=True)
            event(event="end", name=name, status="ok", passes=1, fails=0, **{"skip-mode": 0}, temp_dir=temp)
        elif "skip" in name:
            print(f"[SKIP_TEST] {name} :: require-env NOT_SET", file=sys.stderr, flush=True)
            event(
                event="end", name=name, status="skip-requirement", passes=0, fails=0, **{"skip-mode": 0}, temp_dir=temp
            )
        else:
            event(event="end", name=name, status="ok", passes=1, fails=0, **{"skip-mode": 0}, temp_dir=temp)
    if any("stallatexit" in n for n in names):
        time.sleep(60)
    if any("badexit" in n for n in names):
        return 7
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
