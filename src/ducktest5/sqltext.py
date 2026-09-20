"""SQL as text: split a block into statements, and the sqllogictest text form of a result."""

import datetime
import decimal
import json
import math
import textwrap

_QUOTES = {"'": "'", '"': '"', "`": "`"}


def split_statements(block):
    """The statements of `block`, split on `;` outside quotes, `$$ $$` bodies and comments."""
    statements, start, i, n = [], 0, 0, len(block)
    while i < n:
        c = block[i]
        if c in _QUOTES:
            i += 1
            while i < n:
                if block[i] == c:
                    if i + 1 < n and block[i + 1] == c:  # doubled quote = escaped
                        i += 1
                    else:
                        break
                i += 1
        elif block.startswith("--", i):
            i = block.find("\n", i)
            i = n if i < 0 else i
        elif block.startswith("/*", i):
            i = block.find("*/", i + 2)
            i = n if i < 0 else i + 1
        elif block.startswith("$$", i):
            i = block.find("$$", i + 2)
            i = n if i < 0 else i + 1
        elif c == ";":
            statements.append(block[start:i])
            start = i + 1
        i += 1
    statements.append(block[start:])
    return [s.strip() for s in statements if _has_sql(s)]


def _has_sql(statement):
    return any(line.strip() and not line.strip().startswith("--") for line in statement.splitlines())


class L(str):
    """A SQL literal or expression written by hand, e.g. `L("1::TINYINT")`. It goes into a statement
    as it is. Python knows nothing about what it evaluates to, so rows cannot be derived from it."""


def sql_values(rows):
    """Rows of native values (int, str, date, None, bool) or `L(...)` as a SQL VALUES list."""
    return ", ".join("(" + ", ".join(_sql_literal(v) for v in _as_row(row)) + ")" for row in rows)


def _sql_literal(value):
    if isinstance(value, L):
        return str(value)
    if value is None:
        return "NULL"
    if value is True or value is False:
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        return f"'{render(value)}'::DOUBLE"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, datetime.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, datetime.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TypeError(f"no SQL literal for {value!r}; write it as L('...')")


def _as_row(row):
    return row if isinstance(row, (list, tuple)) else (row,)


def render(value):
    """One value as sqllogictest prints it."""
    if isinstance(value, L):
        raise TypeError(f"values contain L({str(value)!r}); define the rows by hand")
    if value is None:
        return "NULL"
    if value is True or value is False:
        return "true" if value else "false"
    if value == "":
        return "(empty)"
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
        if value == int(value) and abs(value) < 1e15:
            return f"{value:.1f}"
    if isinstance(value, (list, tuple, dict)):
        # One form for a nested value whatever the client's own types: compact JSON.
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    if isinstance(value, (datetime.date, datetime.time, decimal.Decimal, int, float, str)):
        return str(value)
    return str(value)


def render_rows(rows):
    """A row that is not a tuple or list is a row of one value, so `[1, 3]` is two rows."""
    return [tuple(render(v) for v in _as_row(row)) for row in rows]


def parse_rows(text):
    """Expected rows written as text: one line per row, values separated by a TAB."""
    lines = [line for line in textwrap.dedent(text).splitlines() if line.strip()]
    return [tuple(line.strip(" ").split("\t")) for line in lines]


def format_rows(rows):
    return "\n".join("\t".join(row) for row in rows) or "(no rows)"
