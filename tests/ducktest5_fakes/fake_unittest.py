#!/usr/bin/env python3
"""A stand-in for DuckDB's unittest binary, for ducktest5's self-tests. It speaks the same contract:
`-f LIST --test-config JSON --emit-test-events`, one begin and one end event for each test, and
`-l PATTERN` lists. What a test does comes from its file name:

    *fail*   ends with status error        *hang*    never ends
    *crash*  exits before its end event    *skip*    ends as skip-requirement, on its first `require-env`
    *giveup* prints a SKIP_TEST line and ends ok, as the binary does for an error on its skip list
    *onlyinbatch_fail* fails beside other tests and passes alone
    *stallatexit* / *badexit* end ok, then the process hangs / exits 7
    *nap*    takes 0.3 s
    *flakyN* fails until its Nth run, counted in a file under $FAKE_UNITTEST_STATE
    *initfail* fails on the first statement of the init file, which the binary reports -- as the
               real one does -- under the body file's name and the init file's line
    *initfailN* the same, on the Nth statement of the init file
    *showenv* prints the process environment it was started with, for the names in $FAKE_UNITTEST_ENV

As the binary does, a name in the config's `env_passthrough` that is not in its environment fails
the whole invocation before any test.

`-l` lists no names, as a v2.0 build answers the version probe.
"""

import json
import os
import re
import sys
import time


def event(**fields):
    print("[TEST_EVENT] " + json.dumps(fields), file=sys.stderr, flush=True)


RULE = "=" * 80
CATCH_BANNER = "~" * 79 + "\nunittest is a Catch v2.13.7 host application.\nRun with -? for options\n"


def failure(where, *parts):
    """A failure as the real binary frames it: each part between lines of `=`, then Catch's banner."""
    return "\n".join([f"1. {where}", *(f"{RULE}\n{part}" for part in parts), "", CATCH_BANNER])


def runs_so_far(name):
    path = os.path.join(os.environ["FAKE_UNITTEST_STATE"], name.replace("/", "_") + ".runs")
    runs = int(open(path).read()) + 1 if os.path.exists(path) else 1
    with open(path, "w") as f:
        f.write(str(runs))
    return runs


def unmet_env(name):
    """The name of the first `require-env` in the file, if it is there to read."""
    if os.path.isfile(name):
        for line in open(name):
            if line.startswith("require-env "):
                return line.split()[1]
    return "NOT_SET"


def init_statement(config, which=1):
    """The SQL of the `which`th statement of the batch's init file, and the line its header is on."""
    lines = open(config["init_sqllogic"]).read().splitlines()
    headers = [n for n, line in enumerate(lines) if line.startswith(("statement", "query"))]
    header = headers[which - 1]
    sql = []
    for line in lines[header + 1 :]:
        if not line.strip():
            break
        sql.append(line)
    return "\n".join(sql), header + 1


def main(argv):
    if "--version" in argv:  # as the duckdb shell of a build answers; the self-tests give this fake for both
        print("v2.0.0 (fake) 0000000000")
        return 0
    if "-l" in argv:
        print("name\tgroup")
        return 0
    names = [line.strip() for line in open(argv[argv.index("-f") + 1]) if line.strip()]
    config = json.load(open(argv[argv.index("--test-config") + 1]))
    missing = [name for name in config.get("env_passthrough", []) if name not in os.environ]
    if missing:
        print(f"--env-passthrough: not set in the environment: {', '.join(missing)}", flush=True)
        return 1
    failed = False
    for n, name in enumerate(names):
        temp = f"{config['temp_dir_root']}/{config.get('run_id', 'run')}/{name.replace('/', '_')}"
        started = time.perf_counter()
        print(f"[{n}/{len(names)}] ({n * 100 // len(names)}%): {name}", flush=True)
        event(event="begin", name=name, temp_dir=temp)
        if "writes" in name:
            os.makedirs(temp, exist_ok=True)
            open(f"{temp}/out", "w").close()
        if "hang" in name:
            time.sleep(60)
        if "nap" in name:
            time.sleep(0.3)
        if "crash" in name:
            print("Segmentation fault (fake)", flush=True)
            return 139
        if "env" in name:
            print("ENV " + json.dumps({e["env_name"]: e["env_value"] for e in config["test_env"]}), flush=True)
        if "showenv" in name:
            watched = os.environ.get("FAKE_UNITTEST_ENV", "AWS_PROFILE,AWS_ACCESS_KEY_ID").split(",")
            print("PROCESS_ENV " + json.dumps({k: os.environ.get(k) for k in watched}), flush=True)
        if "onlyinbatch" in name and len(names) == 1:
            event(event="end", name=name, status="ok", passes=1, fails=0, **{"skip-mode": 0}, temp_dir=temp)
            continue
        flaky = re.search(r"flaky(\d+)", name)
        if "initfail" in name:
            which = re.search(r"initfail(\d+)", name)
            sql, line = init_statement(config, int(which.group(1)) if which else 1)
            print(
                failure(
                    f"{name}:{line}",
                    f"Query unexpectedly failed! ({name}:{line})!",
                    sql,
                    "Catalog Error: no such thing",
                ),
                flush=True,
            )
            data = f"{name}:{line}"
            event(
                event="end", name=name, status="error", passes=0, fails=1, **{"skip-mode": 0}, temp_dir=temp, data=data
            )
            failed = True
        elif "fail" in name or (flaky and runs_so_far(name) < int(flaky.group(1))):
            print(
                failure(
                    f"{name}:4",
                    f"Wrong result in query! ({name}:4)!",
                    "SELECT 1;",
                    "Mismatch on row 1, column 1(index 1)\n1 <> 2",
                    "Expected result:",
                    "2\n",
                    "Actual result:",
                    "1\n",
                ),
                flush=True,
            )
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
            print(f"[SKIP_TEST] {name} :: require-env {unmet_env(name)}", file=sys.stderr, flush=True)
            event(
                event="end", name=name, status="skip-requirement", passes=0, fails=0, **{"skip-mode": 0}, temp_dir=temp
            )
        else:
            event(event="end", name=name, status="ok", passes=1, fails=0, **{"skip-mode": 0}, temp_dir=temp)
        # As the binary does: the only place it says how long one test took.
        print(
            f"[{n + 1}/{len(names)}] ({(n + 1) * 100 // len(names)}%): {name} took {time.perf_counter() - started:.3f}s",
            flush=True,
        )
    if any("stallatexit" in n for n in names):
        time.sleep(60)
    if any("badexit" in n for n in names):
        return 7
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
