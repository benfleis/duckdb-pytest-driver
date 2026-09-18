"""One SQL vocabulary for every client a body drives, DuckDB included. The verbs are sqllogictest's:

    ok(sql)             statement ok      must succeed
    expect(sql, rows)   query + ----      the assertion, expected rows beside the query
    error(sql, text)    statement error   must fail, with `text` in the message

`sql` is one statement, a block of them split on `;`, or a list of statements taken as they are (for
a statement the splitter would cut, such as a scripted BEGIN ... END). Every statement must succeed,
and `rows` / `text` belong to the last one. `{t}` is this client's spelling of the table under test; other
`{name}` placeholders come from keyword arguments. Braces that are not a known placeholder are left
alone (struct literals), so write plain strings, not f-strings.

Rows compare as TEXT, the way a .test file does: one line per row, TAB between values, `NULL`,
`(empty)`, `true`/`false`. `rows` is written in that form; a list of tuples is a second spelling of
the same thing, rendered by the same rules before the comparison, so `[(1, None, "")]` means exactly
`1<TAB>NULL<TAB>(empty)`, and `[1, 3]` is two rows of one value. Pick the spelling that reads best
at that place. The text form is the only comparison: 1 and '1' are equal, as in a .test
file. Where the type matters, ask in SQL (`SELECT typeof(x)`). DuckDB, Spark and Databricks SQL all
have `assert_true(cond[, message])`, so a statement can check itself.
"""

import re

from .sqltext import format_rows, parse_rows, render_rows, split_statements


class SqlError(Exception):
    """`statement` is the index, in the list given to `run`, of the statement that failed."""

    def __init__(self, message, statement=None):
        super().__init__(message)
        self.statement = statement


class ExpectationFailed(AssertionError):
    def __init__(self, client, sql, problem):
        super().__init__(f"[{client}] {problem}\n{_indent(sql)}")


def _indent(text, by="    "):
    return "\n".join(by + line for line in text.strip("\n").splitlines())


def _as_text(rows):
    return parse_rows(rows) if isinstance(rows, str) else render_rows(rows)


class Client:
    def __init__(self, name, run, *, table=None):
        """`run(statements)` runs them in order and returns `(column_names, rows)` of the last,
        rows as tuples; it raises SqlError, with the failed statement's index, on the first error."""
        self.name = name
        self._run = run
        self._params = {"t": table} if table else {}

    def _statements(self, sql, params):
        known = {**self._params, **params}

        def fill(text):
            return re.sub(r"\{(\w+)\}", lambda m: str(known[m.group(1)]) if m.group(1) in known else m.group(0), text)

        return split_statements(fill(sql)) if isinstance(sql, str) else [fill(s).strip() for s in sql]

    def _must_run(self, statements, what):
        try:
            return self._run(statements)
        except SqlError as exc:
            failed = statements[exc.statement] if exc.statement is not None else "\n".join(statements)
            raise ExpectationFailed(self.name, failed, f"{what} failed: {exc}") from None

    def rows(self, sql, **params):
        return self._must_run(self._statements(sql, params), "query")[1]

    def record(self, sql, **params):
        """The single row of `sql` as a dict, for DESCRIBE-like statements with many columns."""
        statements = self._statements(sql, params)
        columns, rows = self._must_run(statements, "query")
        assert len(rows) == 1, f"[{self.name}] expected one row, got {len(rows)}\n{_indent(statements[-1])}"
        return dict(zip(columns, rows[0]))

    def ok(self, sql, **params):
        self._must_run(self._statements(sql, params), "statement")

    def expect(self, sql, rows, *, sort=False, **params):
        statements = self._statements(sql, params)
        expected, actual = _as_text(rows), render_rows(self._must_run(statements, "query")[1])
        if sort:
            expected, actual = sorted(expected), sorted(actual)
        if actual == expected:
            return
        widths = {len(r) for r in expected}, {len(r) for r in actual}
        hint = ""
        if actual and expected and widths[0] != widths[1]:
            hint = f" (expected rows have {sorted(widths[0])} values, the result has {sorted(widths[1])}; TAB between values?)"
        raise ExpectationFailed(
            self.name,
            statements[-1],
            f"wrong result{hint}\n  expected:\n{_indent(format_rows(expected))}\n  actual:\n{_indent(format_rows(actual))}\n  query:",
        )

    def expect_contains(self, sql, rows, **params):
        """Every row of `rows` is in the result; the result may hold more."""
        statements = self._statements(sql, params)
        actual = render_rows(self._must_run(statements, "query")[1])
        missing = [r for r in _as_text(rows) if r not in actual]
        if missing:
            raise ExpectationFailed(
                self.name,
                statements[-1],
                f"rows missing\n  missing:\n{_indent(format_rows(missing))}\n  actual:\n{_indent(format_rows(actual))}\n  query:",
            )

    def error(self, sql, text, **params):
        statements = self._statements(sql, params)
        try:
            self._run(statements)
        except SqlError as exc:
            if exc.statement not in (None, len(statements) - 1):
                raise ExpectationFailed(self.name, statements[exc.statement], f"statement failed: {exc}") from None
            if text not in str(exc):
                raise ExpectationFailed(self.name, statements[-1], f"failed, but not with {text!r}: {exc}") from None
            return
        raise ExpectationFailed(self.name, statements[-1], f"statement succeeded, expected an error with {text!r}")
