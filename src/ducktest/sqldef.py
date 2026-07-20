"""Generic multi-statement SQL-def core: quote-aware splitting, literal rendering,
INSERT building, and a substitution-driven file runner.

No engine/SDK dependency -- `run_sql_file` takes a caller-provided `execute` callable
and a generic `subs` dict, so any extension (databricks, iceberg, ...) can layer its own
domain wrapper on top.
"""


def split_statements(sql):
    """Split multi-statement SQL on TOP-LEVEL `;` only.

    A `;` inside a 'single-quoted' string is not a boundary (`''` is an escaped quote).
    Line comments are stripped by `run_sql_file`. The SQL API has no multi-statement call,
    so this client-side split is unavoidable; being quote-aware is what keeps it honest.
    """
    out, buf, in_str = [], [], False
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            buf.append(c)
            if in_str and i + 1 < n and sql[i + 1] == "'":  # '' -> escaped quote, stay in string
                buf.append("'")
                i += 2
                continue
            in_str = not in_str
        elif c == ";" and not in_str:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(c)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        out.append(stmt)
    return out


def sql_literal(v):
    """Render a Python scalar as a SQL literal (for INSERT ... VALUES)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def build_insert(fqn, rows, *, columns=None):
    """Build an INSERT INTO ... VALUES statement from `rows` (list of python-scalar tuples)."""
    cols = f" ({columns})" if columns else ""
    values = ", ".join("(" + ", ".join(sql_literal(v) for v in row) + ")" for row in rows)
    return f"INSERT INTO {fqn}{cols} VALUES {values}"


def run_sql_file(path, execute, *, subs=None, dry_run=False):
    """Run a raw multi-statement SQL def file verbatim through `execute`.

    Strips full-line `--` comments, applies `{key}` substitution for every key in `subs`,
    splits quote-aware on `;`, and calls `execute(stmt)` for each statement (unless dry_run).
    Returns the statement list (executed unless dry_run).
    """
    with open(path) as f:
        text = f.read()
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    for k, v in (subs or {}).items():
        text = text.replace("{" + k + "}", v)
    statements = split_statements(text)
    if not dry_run:
        for stmt in statements:
            execute(stmt)
    return statements
