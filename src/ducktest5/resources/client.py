"""One SQL vocabulary for every client a body drives, DuckDB included.

    setup(sql)             not under test; a failure reports as a setup ERROR, not a FAILED test
    oks(sql)               statement ok      under test; must succeed
    errs(sql, text)        statement error   under test; must fail, with `text` in the message
    expects(sql, rows)     query + ----      under test; expected rows beside the query
    exec(sql)              no verdict; the body reads the answer and decides (a retry loop)
    teardown(sql)          runs after the body, even when the test aborted; a failure is an ERROR

A failure aborts the test, as in a .test file. `on_fail="continue"` on `oks`, `errs` or `expects`
records the failure and lets the body go on, for independent checks a reader wants to see together.

`sql` is one statement, a block of them split on `;`, or a list of statements taken as they are (for
a statement the splitter would cut, such as a scripted BEGIN ... END). Every statement must succeed,
and `rows` / `text` belong to the last one. `{t}` is this client's spelling of the table under test;
other `{name}` placeholders come from the test's values (`ctx.values`, what a `.test` file reads as
`{NAME}`), from `params=` (a dict: a params row goes there), or from keyword arguments; an
unknown one is an error, and `{{word}}` is a literal brace pair. A value that is a list of rows goes
in as a SQL VALUES list, so the values a test inserts and the rows it expects back are one definition
(`L("...")` puts hand-written SQL in a value; rows can then not be derived and must be given). Braces
around anything but a name are left alone (struct literals). Write plain strings: an f-string as
the SQL of a verb is refused when its file is imported, since it fills the template once, in Python,
where the client fills it per engine; give the name a value (`ctx.values.set`, a rule's `values=`, or
a keyword of the call) instead.
A value that holds `{name}`s is filled first, from the other values, each once
(``t="delta.`{TEMP_DIR}/t`"`` works; values that refer to each other in a cycle are an error), and
the SQL is then filled in one pass, so what a value brings in is never read again.

Names are case-insensitive. Python writes them lower case and a `.test` file upper case, so `{path}`
and `{PATH}` are one name with one value. Two names given together that differ only in case are an
error; a later layer (the client's own `values` over the test's, then the call's) replaces a name in any
case.

Rows compare as TEXT, the way a .test file does: one line per row, TAB between values, `NULL`,
`(empty)`, `true`/`false`. `rows` is written in that form; a list of tuples is a second spelling of
the same thing, rendered by the same rules before the comparison, so `[(1, None, "")]` means exactly
`1<TAB>NULL<TAB>(empty)`, and `[1, 3]` is two rows of one value. Rows that are not a list or text (a
bare value, a single tuple) are refused: they could mean one row or several. Pick the spelling that
reads best at that place. The text form is the only comparison: 1 and '1' are equal, as in a .test file. Where
the type matters, ask in SQL (`SELECT typeof(x)`). DuckDB, Spark and Databricks SQL all have
`assert_true(cond[, message])`, so a statement can check itself.
"""

import logging
import re
import time
import traceback
from collections import Counter
from collections.abc import Mapping

from ..binary import UsageError
from ..sqltext import format_rows, parse_rows, render_rows, split_statements, sql_values

_log = logging.getLogger(__name__)

ON_FAIL = ("abort_test", "continue")
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}|\{(\w+)\}")


class SqlError(Exception):
    """`statement` is the index, in the list given to `run`, of the statement that failed."""

    def __init__(self, message, statement=None):
        super().__init__(message)
        self.statement = statement


class ExpectationFailed(AssertionError):
    """`expected` and `actual` are the rows as text where the check compared rows; a report lays
    them out side by side instead of leaving the reader to find them in the message."""

    def __init__(self, client, sql, problem, *, expected=None, actual=None):
        super().__init__(f"[{client}] {problem}\n{_indent(sql)}")
        self.stack = traceback.extract_stack()[:-1]  # a recorded check has no traceback to read later
        self.sql, self.expected, self.actual = sql, expected, actual


class SetupFailed(Exception):
    """Something the test needs, but does not test, failed."""


class Executed:
    """What `exec` gives back: truthy when the statements ran, `error` the engine's message when
    the last one failed, `columns` and `rows` its result when it did not."""

    def __init__(self, client, sql, *, error=None, columns=(), rows=()):
        self.client, self.sql, self.error = client, sql, error
        self.columns, self.rows = list(columns), list(rows)

    def __bool__(self):
        return self.error is None

    def __repr__(self):
        what = f"failed: {self.error}" if self.error is not None else f"ok, {len(self.rows)} row(s)"
        return f"[{self.client}] exec, {what}\n{_indent(self.sql)}"


def _indent(text, by="    "):
    return "\n".join(by + line for line in text.strip("\n").splitlines())


def _as_text(rows):
    if isinstance(rows, str):
        return parse_rows(rows)
    if not isinstance(rows, list):
        # A tuple could be one row or several rows of one value, and a scalar is not rows at all.
        one = render_rows([rows])[0]
        raise UsageError(
            f"rows={rows!r} is not rows. Rows are a list of rows, rows=[{rows!r}], "
            f"or text with a TAB between values, rows={chr(9).join(one)!r}"
        )
    return render_rows(rows)


def check_names(given, where="values"):
    """`given`, when no two of its names differ only in case; `where` names what gave them."""
    spelled = {}
    for name in given:
        if name.lower() in spelled:
            raise UsageError(
                f"{where}: {spelled[name.lower()]!r} and {name!r} are one name; names are case-insensitive"
            )
        spelled[name.lower()] = name
    return given


class Names(dict):
    """Template names, kept lower case: `{path}` and `{PATH}` look up one entry."""

    def __init__(self, given=(), where="values"):
        super().__init__()
        for name, value in check_names(dict(given), where).items():
            super().__setitem__(name.lower(), value)

    def __getitem__(self, name):
        return super().__getitem__(name.lower())

    def __setitem__(self, name, value):
        super().__setitem__(name.lower(), value)

    def __contains__(self, name):
        return isinstance(name, str) and super().__contains__(name.lower())

    def get(self, name, default=None):
        return super().get(name.lower(), default)

    def update(self, given=(), **more):
        for layer in (given, more):
            for key, value in Names(layer).items():
                super().__setitem__(key, value)


class Values(Mapping):
    """The `{NAME}` values a template sees, by any case of the name: `values.path`, `values["PATH"]`.
    `set(name=value)` adds or replaces values from now on. A Values made over a `parent` sees what
    the parent has, what is set there later too, and keeps what is set on it to itself.

    A name that is also a method here (`get`, `items`, `set`, ...) is read as `values["name"]`."""

    def __init__(self, given=(), parent=None):
        self._own = Names(given)
        self._parent = parent

    def set(self, **values):
        self._own.update(values)
        return self

    def names(self) -> Names:
        """Every name it sees, with its value, the parent's first."""
        out = self._parent.names() if self._parent is not None else Names()
        out.update(self._own)
        return out

    def __getitem__(self, name):
        if name in self._own:
            return self._own[name]
        if self._parent is not None:
            return self._parent[name]
        raise KeyError(name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"values has no {name!r}; it has {sorted(self)}") from None

    def __iter__(self):
        return iter(self.names())

    def __len__(self):
        return len(self.names())

    def __contains__(self, name):
        return name in self._own or (self._parent is not None and name in self._parent)

    def __repr__(self):
        return f"Values({dict(self.names())!r})"


class Client:
    """One engine's lease for one test: the verbs, run on the engine instance the test holds.
    `engine` is that instance, or None for a client of a body's own (a shell nobody else uses)."""

    def __init__(self, name, run, *, table=None, values=None, record=None, defer=None, log=None, engine=None):
        """`run(statements)` runs them in order and returns `(column_names, rows)` of the last,
        rows as tuples; it raises SqlError, with the failed statement's index, on the first error.
        `values` are the test's: this client's own `values` are over them, so what the test sets later is
        seen too, and what is set on this client stays with it. `record(failure)` takes a failure under `on_fail="continue"`;
        `defer(callable)` takes a teardown. Without them such a failure raises and a teardown runs
        at once. `log(entry)` takes one `{client, sql, seconds}` for every call, which a verbose
        report prints."""
        self.name = name
        self.engine = engine
        self._run = run
        self.values = Values({"t": table} if table else {}, parent=values)
        self._record = record
        self._defer = defer
        self._log = log
        self.seconds = 0.0  # spent inside `run`, so a report can say which client the time went to

    @property
    def dialect(self):
        return self.engine.dialect if self.engine is not None else None

    def _check(self, on_fail, assertion, *args, **params):
        if on_fail not in ON_FAIL:
            raise ValueError(f"on_fail={on_fail!r}; it is one of {ON_FAIL}")
        try:
            assertion(*args, **params)
        except ExpectationFailed as failure:
            if on_fail == "abort_test" or self._record is None:
                raise
            self._record(failure)

    def _statements(self, sql, params, more):
        """`params` is a dict of placeholder values (a params row goes here, so its keys cannot collide
        with a verb's own options); `more` are the same as keywords, for the odd single value."""
        known = self.values.names()
        for layer in (params or {}, more):
            known.update(layer)

        filled = {}  # lower-case name -> its value with the names inside it filled

        def resolved(name, chain):
            key = name.lower()
            if key not in filled:
                if key in chain:
                    cycle = " -> ".join(f"{{{n}}}" for n in [*chain[chain.index(key) :], key])
                    raise UsageError(f"[{self.name}] the values refer to each other in a cycle: {cycle}")
                v = known[name]
                if chain:
                    _log.debug("[%s] resolving {%s} for {%s}, in order", self.name, name, chain[-1])
                filled[key] = fill(v, [*chain, key]) if isinstance(v, str) else v
            return filled[key]

        def fill(text, chain=()):
            def value(match):
                if match.group(1):  # {{word}}: a literal brace pair around a word
                    return "{" + match.group(1) + "}"
                name = match.group(2)
                if name not in known:
                    inside = f" (in the value of {{{chain[-1]}}})" if chain else ""
                    raise UsageError(
                        f"[{self.name}] no value for {{{name}}}{inside}; known: {sorted(known)}. "
                        "A literal brace pair around a word is written {{word}}."
                    )
                v = resolved(name, list(chain))
                return sql_values(v) if isinstance(v, (list, tuple)) else str(v)

            return _PLACEHOLDER.sub(value, text)

        return split_statements(fill(sql)) if isinstance(sql, str) else [fill(x).strip() for x in sql]

    def _timed_run(self, statements):
        t0 = time.perf_counter()
        try:
            return self._run(statements)
        finally:
            took = time.perf_counter() - t0
            self.seconds += took
            if self._log:
                self._log({"client": self.name, "sql": "\n".join(statements), "seconds": round(took, 3)})

    def _must_run(self, statements, what):
        try:
            return self._timed_run(statements)
        except SqlError as exc:
            failed = statements[exc.statement] if exc.statement is not None else "\n".join(statements)
            raise ExpectationFailed(self.name, failed, f"{what} failed: {exc}") from None

    def _not_from_the_last_statement(self, statements, exc):
        """An earlier statement, the preamble, a timeout or a client that died: whatever the call
        asks about the last statement, this failure is no answer to it."""
        if exc.statement == len(statements) - 1:
            return
        failed = statements[exc.statement] if exc.statement is not None else "(not a statement of this call)"
        raise ExpectationFailed(self.name, failed, f"statement failed: {exc}") from None

    def rows(self, sql, *, params=None, **more):
        return self._must_run(self._statements(sql, params, more), "query")[1]

    def record(self, sql, *, params=None, **more):
        """The single row of `sql` as a dict, for DESCRIBE-like statements with many columns."""
        statements = self._statements(sql, params, more)
        columns, rows = self._must_run(statements, "query")
        assert len(rows) == 1, f"[{self.name}] expected one row, got {len(rows)}\n{_indent(statements[-1])}"
        return dict(zip(columns, rows[0]))

    def setup(self, sql, *, params=None, **more):
        try:
            self._assert_ok(sql, params, **more)
        except ExpectationFailed as failure:
            raise SetupFailed(str(failure)) from None

    def teardown(self, sql, *, params=None, **more):
        def run():
            try:
                self._assert_ok(sql, params, **more)
            except ExpectationFailed as failure:
                raise SetupFailed(str(failure)) from None

        self._defer(run) if self._defer else run()

    def oks(self, sql, *, params=None, on_fail="abort_test", **more):
        self._check(on_fail, self._assert_ok, sql, params, **more)

    def errs(self, sql, text, *, params=None, on_fail="abort_test", **more):
        self._check(on_fail, self._assert_error, sql, text, params, **more)

    def expects(self, sql, rows, *, params=None, sort=False, on_fail="abort_test", **more):
        self._check(on_fail, self._assert_rows, sql, rows, params, sort=sort, **more)

    def exec(self, sql, *, params=None, **more):
        """Run the statements and give back what happened, instead of judging it: for a body that has
        to branch, such as a retry loop around a commit that can conflict. A statement that failed is
        not a failed check, and does not abort the test."""
        statements = self._statements(sql, params, more)
        try:
            columns, rows = self._timed_run(statements)
        except SqlError as exc:
            self._not_from_the_last_statement(statements, exc)
            return Executed(self.name, statements[-1], error=str(exc))
        return Executed(self.name, statements[-1], columns=columns, rows=rows)

    def expects_contains(self, sql, rows, *, params=None, on_fail="abort_test", **more):
        """Every row of `rows` is in the result, as often as it is in `rows`; the result may hold more."""
        self._check(on_fail, self._assert_contains, sql, rows, params, **more)

    def _assert_ok(self, sql, params=None, **more):
        self._must_run(self._statements(sql, params, more), "statement")

    def _assert_rows(self, sql, rows, params=None, *, sort=False, **more):
        statements = self._statements(sql, params, more)
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
            expected=format_rows(expected),
            actual=format_rows(actual),
        )

    def _assert_contains(self, sql, rows, params=None, **more):
        statements = self._statements(sql, params, more)
        actual = render_rows(self._must_run(statements, "query")[1])
        missing = list((Counter(_as_text(rows)) - Counter(actual)).elements())
        if missing:
            raise ExpectationFailed(
                self.name,
                statements[-1],
                f"rows missing\n  missing:\n{_indent(format_rows(missing))}\n  actual:\n{_indent(format_rows(actual))}\n  query:",
                expected=format_rows(missing),
                actual=format_rows(actual),
            )

    def _assert_error(self, sql, text, params=None, **more):
        statements = self._statements(sql, params, more)
        try:
            self._timed_run(statements)
        except SqlError as exc:
            self._not_from_the_last_statement(statements, exc)
            if text not in str(exc):
                raise ExpectationFailed(self.name, statements[-1], f"failed, but not with {text!r}: {exc}") from None
            return
        raise ExpectationFailed(self.name, statements[-1], f"statement succeeded, expected an error with {text!r}")
