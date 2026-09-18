"""SQL as text: split a block into statements, and the sqllogictest text form of a result."""

import datetime
import decimal
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


def render(value):
    """One value as sqllogictest prints it."""
    if value is None:
        return "NULL"
    if value is True or value is False:
        return "true" if value else "false"
    if value == "":
        return "(empty)"
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return f"{value:.1f}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(v) for v in value) + "]"
    if isinstance(value, (datetime.date, datetime.time, decimal.Decimal, int, float, str)):
        return str(value)
    return str(value)


def render_rows(rows):
    """A row that is not a tuple or list is a row of one value, so `[1, 3]` is two rows."""
    return [tuple(render(v) for v in (row if isinstance(row, (list, tuple)) else (row,))) for row in rows]


def parse_rows(text):
    """Expected rows written as text: one line per row, values separated by a TAB."""
    lines = [line for line in textwrap.dedent(text).splitlines() if line.strip()]
    return [tuple(line.strip(" ").split("\t")) for line in lines]


def format_rows(rows):
    return "\n".join("\t".join(row) for row in rows) or "(no rows)"
