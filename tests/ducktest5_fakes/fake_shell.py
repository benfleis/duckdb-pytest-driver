#!/usr/bin/env python3
"""A stand-in for the duckdb shell, for the protocol of ducktest5's DuckDB session: it reads lines,
answers `.print X` with X, and a statement that holds a word below with what the word says:

    drip    a line of output every 50 ms, for 5 s
    echoN   the line `[{"v":N}]`, after 20 ms
"""

import re
import sys
import time

for line in sys.stdin:
    if line.startswith(".print "):
        print(line[len(".print ") :].strip(), flush=True)
    elif "drip" in line:
        for _ in range(100):
            print("drop", flush=True)
            time.sleep(0.05)
    elif m := re.search(r"echo(\d+)", line):
        time.sleep(0.02)
        print('[{"v":' + m.group(1) + "}]", flush=True)
