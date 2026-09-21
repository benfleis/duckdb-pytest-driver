#!/usr/bin/env python3
"""A stand-in for DuckDB's unittest binary, for ducktest5's self-tests. It speaks the same contract:
`-f LIST --test-config JSON --emit-test-events`, one begin and one end event for each test, and
`-l PATTERN` lists. What a test does comes from its file name:

    *fail*   ends with status error        *hang*    never ends
    *crash*  exits before its end event    *skip*    ends as skip-requirement
    *giveup* prints a SKIP_TEST line and ends ok, as the binary does for an error on its skip list
    *onlyinbatch_fail* fails beside other tests and passes alone
    *stallatexit* / *badexit* end ok, then the process hangs / exits 7
    *nap*    takes 0.3 s
    *flakyN* fails until its Nth run, counted in a file under $FAKE_UNITTEST_STATE
    *initfail* fails on the first statement of the init file, which the binary reports -- as the
               real one does -- under the body file's name and the init file's line

`-l` lists the names in the file that $FAKE_UNITTEST_LIST names.
"""

import json
import os
import re
import sys
import time


def event(**fields):
    print("[TEST_EVENT] " + json.dumps(fields), file=sys.stderr, flush=True)


def runs_so_far(name):
    path = os.path.join(os.environ["FAKE_UNITTEST_STATE"], name.replace("/", "_") + ".runs")
    runs = int(open(path).read()) + 1 if os.path.exists(path) else 1
    with open(path, "w") as f:
        f.write(str(runs))
    return runs


def init_statement(config):
    """The SQL of the first statement of the batch's init file, and the line its header is on."""
    lines = open(config["init_sqllogic"]).read().splitlines()
    header = next(n for n, line in enumerate(lines) if line.startswith(("statement", "query")))
    sql = []
    for line in lines[header + 1 :]:
        if not line.strip():
            break
        sql.append(line)
    return "\n".join(sql), header + 1


def main(argv):
    if "-l" in argv:
        print("name\tgroup")
        for line in open(os.environ["FAKE_UNITTEST_LIST"]) if "FAKE_UNITTEST_LIST" in os.environ else []:
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
        if "nap" in name:
            time.sleep(0.3)
        if "crash" in name:
            print("Segmentation fault (fake)", flush=True)
            return 139
        if "env" in name:
            print("ENV " + json.dumps({e["env_name"]: e["env_value"] for e in config["test_env"]}), flush=True)
        if "onlyinbatch" in name and len(names) == 1:
            event(event="end", name=name, status="ok", passes=1, fails=0, **{"skip-mode": 0}, temp_dir=temp)
            continue
        flaky = re.search(r"flaky(\d+)", name)
        if "initfail" in name:
            sql, line = init_statement(config)
            print(f"Query unexpectedly failed! ({name}:{line})!\n{sql}\nCatalog Error: no such thing", flush=True)
            data = f"{name}:{line}"
            event(
                event="end", name=name, status="error", passes=0, fails=1, **{"skip-mode": 0}, temp_dir=temp, data=data
            )
            failed = True
        elif "fail" in name or (flaky and runs_so_far(name) < int(flaky.group(1))):
            print(f"Wrong result in query! ({name}:4)\nExpected result:\n2\nActual result:\n1", flush=True)
            event(
                event="end",
                name=name,
                status="error",
                passes=0,
                fails=1,
                **{"skip-mode": 0},
                temp_dir=temp,
                data=f"{name}:4",
            )
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
