#!/usr/bin/env python3
"""A stand-in for the duckdb shell, for the protocol of ducktest5's DuckDB session: it reads lines,
answers `.print X` with X, and a statement that holds a word below with what the word says:

    drip    a line of output every 50 ms, for 5 s
    echoN   the line `[{"v":N}]`, after 20 ms

Any other statement is read up to the `;` line after it. A `CREATE SECRET` is kept until the next
`.open`, and a statement that reads `duckdb_secrets()` gets what is kept. `count(*)` of a
`read_csv('http://...')` fetches the file and counts its rows. `COPY (SELECT ...) TO 'path' (FORMAT
csv)` writes a local file with a header, and `SELECT col FROM read_csv('path')` reads one column of
it back. Any other `SELECT` is answered by SQLite, which is enough for a constant; SQLite has no
booleans, so the result of a `LIKE` is given as one.

With FAKE_SHELL_RECORD set, every line it reads is appended to that file.
"""

import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

_SECRET = re.compile(r"CREATE SECRET (\w+) \((.*)\)", re.S)
_OPTION = re.compile(r"(\w+) (?:'((?:[^']|'')*)'|(\w+))")
_COUNT_CSV = re.compile(r"SELECT count\(\*\) FROM read_csv\('(http://[^']+)'\)", re.I)
_COPY_CSV = re.compile(r"COPY \((SELECT .*)\) TO '([^']+)' \(FORMAT csv\)", re.I | re.S)
_READ_CSV = re.compile(r"SELECT (\w+) FROM read_csv\('(/[^']+)'\)", re.I)


def answer(sql, secrets):
    if m := _SECRET.match(sql):
        options = {k: (v or word).replace("''", "'") for k, v, word in _OPTION.findall(m.group(2))}
        secrets.append({"name": m.group(1), "type": options["TYPE"], "scope": [options["SCOPE"]]})
    elif m := _COUNT_CSV.match(sql):
        with urllib.request.urlopen(m.group(1), timeout=5) as response:
            rows = response.read().decode().splitlines()[1:]
        print(json.dumps([{"count_star()": len(rows)}]), flush=True)
    elif m := _COPY_CSV.match(sql):
        cursor = sqlite3.connect(":memory:").execute(m.group(1))
        with open(m.group(2), "w", newline="") as f:
            csv.writer(f).writerows([[d[0] for d in cursor.description], *cursor])
    elif m := _READ_CSV.match(sql):
        with open(m.group(2), newline="") as f:
            rows = list(csv.DictReader(f))
        print(json.dumps([{m.group(1): int(r[m.group(1)])} for r in rows]), flush=True)
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
        if " LIKE " in sql.upper():
            rows = [{k: bool(v) for k, v in row.items()} for row in rows]
        if rows:
            print(json.dumps(rows), flush=True)


if "--version" in sys.argv:
    print("v2.0.0 (fake) 0000000000")
    sys.exit(0)
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
