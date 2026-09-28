#!/usr/bin/env python3
"""A stand-in for the duckdb shell, for the protocol of ducktest5's DuckDB session: it reads lines,
answers `.print X` with X, and a statement that holds a word below with what the word says:

    drip    a line of output every 50 ms, for 5 s
    echoN   the line `[{"v":N}]`, after 20 ms

Any other statement is read up to the `;` line after it. A `CREATE SECRET` is kept until the next
`.open`, and a statement that reads `duckdb_secrets()` gets what is kept. Any other `SELECT` is
answered by SQLite, which is enough for a constant.

With FAKE_SHELL_RECORD set, every line it reads is appended to that file.
"""

import json
import os
import re
import sqlite3
import sys
import time

_SECRET = re.compile(r"CREATE SECRET (\w+) \((.*)\)", re.S)
_OPTION = re.compile(r"(\w+) '((?:[^']|'')*)'")


def answer(sql, secrets):
    if m := _SECRET.match(sql):
        options = {k: v.replace("''", "'") for k, v in _OPTION.findall(m.group(2))}
        secrets.append({"name": m.group(1), "type": options["TYPE"], "scope": [options["SCOPE"]]})
    elif "duckdb_secrets()" in sql:
        if secrets:
            print(json.dumps(secrets), flush=True)
    elif sql.upper().startswith("SELECT"):
        try:
            cursor = sqlite3.connect(":memory:").execute(sql)
        except sqlite3.Error as exc:
            print(f"Parser Error: {exc}", flush=True)
            return
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor]
        if rows:
            print(json.dumps(rows), flush=True)


record = os.environ.get("FAKE_SHELL_RECORD")
secrets, statement = [], []
for line in sys.stdin:
    if record:
        with open(record, "a") as f:
            f.write(line)
    if line.startswith(".print "):
        print(line[len(".print ") :].strip(), flush=True)
    elif line.startswith(".open"):
        secrets.clear()
    elif "drip" in line:
        for _ in range(100):
            print("drop", flush=True)
            time.sleep(0.05)
    elif m := re.search(r"echo(\d+)", line):
        time.sleep(0.02)
        print('[{"v":' + m.group(1) + "}]", flush=True)
    elif line.strip() == ";":
        answer(" ".join(statement).strip(), secrets)
        statement = []
    else:
        statement.append(line.strip())
