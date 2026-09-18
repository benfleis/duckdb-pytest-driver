"""One SQL vocabulary for every client a body drives, DuckDB included. The verbs are sqllogictest's:

    ok(sql)             statement ok      setup or action; must succeed
    expect(sql, rows)   query + ----      the assertion, expected rows beside the query
    error(sql, text)    statement error   must fail, with `text` in the message

`{t}` in a statement is this client's spelling of the table under test; other `{name}` placeholders
come from keyword arguments. Braces that are not a known placeholder are left alone (struct literals).
"""

import re


class SqlError(Exception):
    pass


class ExpectationFailed(AssertionError):
    def __init__(self, client, sql, problem):
        super().__init__(f"[{client}] {problem}\n{_indent(sql)}")


def _indent(text):
    return "\n".join("    " + line for line in text.strip().splitlines())


def _table(rows):
    return _indent("\n".join(repr(r) for r in rows) or "(no rows)")


class Client:
    def __init__(self, name, run, *, table=None):
        """`run(sql)` returns rows as a list of tuples, or raises SqlError."""
        self.name = name
        self._run = run
        self._params = {"t": table} if table else {}

    def _render(self, sql, params):
        known = {**self._params, **params}
        return re.sub(r"\{(\w+)\}", lambda m: str(known[m.group(1)]) if m.group(1) in known else m.group(0), sql)

    def rows(self, sql, **params):
        return self._run(self._render(sql, params))

    def ok(self, sql, **params):
        sql = self._render(sql, params)
        try:
            self._run(sql)
        except SqlError as exc:
            raise ExpectationFailed(self.name, sql, f"statement failed: {exc}") from None

    def expect(self, sql, rows, **params):
        sql = self._render(sql, params)
        try:
            actual = self._run(sql)
        except SqlError as exc:
            raise ExpectationFailed(self.name, sql, f"query failed: {exc}") from None
        expected = [tuple(r) for r in rows]
        if actual != expected:
            raise ExpectationFailed(
                self.name, sql, f"wrong result\n  expected:\n{_table(expected)}\n  actual:\n{_table(actual)}\n  query:"
            )

    def expect_contains(self, sql, rows, **params):
        """Every row of `rows` is in the result; the result may hold more."""
        sql = self._render(sql, params)
        try:
            actual = self._run(sql)
        except SqlError as exc:
            raise ExpectationFailed(self.name, sql, f"query failed: {exc}") from None
        missing = [tuple(r) for r in rows if tuple(r) not in actual]
        if missing:
            raise ExpectationFailed(
                self.name, sql, f"rows missing\n  missing:\n{_table(missing)}\n  actual:\n{_table(actual)}\n  query:"
            )

    def error(self, sql, text, **params):
        sql = self._render(sql, params)
        try:
            self._run(sql)
        except SqlError as exc:
            if text not in str(exc):
                raise ExpectationFailed(self.name, sql, f"failed, but not with {text!r}: {exc}") from None
            return
        raise ExpectationFailed(self.name, sql, f"statement succeeded, expected an error with {text!r}")
