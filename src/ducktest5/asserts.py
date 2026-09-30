"""A failed `assert` in a test body says what the two sides were.

`assert ids == list(range(1, len(ids) + 1))` raises an AssertionError that tells a reader nothing, so
a test file is rewritten while it is imported: an `assert` keeps the values it compared and prints
them, a list of rows the way a failed `expects` prints one, with a diff when there is more than a
line. A bare `assert x` prints `x`. Modules the test file imports from inside the test tree get the
same treatment, so a `check()` beside the tests reads like one written in the file.

The rewrite swaps one statement for a few at the same line, so a traceback still points at the
`assert` the author wrote.

The same pass refuses an f-string written as the SQL of a verb (`oks(f"... {x}")`): a template is
filled per engine, from the test's env, and an f-string fills it once, in Python, before any engine
sees it.
"""

from __future__ import annotations

import ast
import datetime
import decimal
import difflib
import importlib.machinery
import importlib.util
import os
import pprint
import sys

from .binary import UsageError
from .sqltext import L, format_rows, render_rows

# None of these is a legal identifier, so nothing a test file writes can collide with them.
_HELPER = "@ducktest_asserts"
_LEFT, _RIGHT, _VALUE = "@ducktest_left", "@ducktest_right", "@ducktest_value"

_OPS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not",
}
_MAX_LINES = 40  # of one side; a report file is read by a person
_ONE_LINE = 100

_SCALARS = (str, int, float, bool, bytes, datetime.date, datetime.time, decimal.Decimal, type(None))


# -- what a failure says ------------------------------------------------------


def failed_comparison(source, op, left, right, message=None):
    """The AssertionError of a failed `assert a OP b`, with both sides."""
    lines = ([str(message)] if message is not None else []) + [f"assert {source}"]
    left_lines, right_lines = _lines(left), _lines(right)
    if len(left_lines) == 1 and len(right_lines) == 1 and len(left_lines[0]) + len(right_lines[0]) <= _ONE_LINE:
        return AssertionError("\n".join(lines + [f"  left:  {left_lines[0]}", f"  right: {right_lines[0]}"]))
    lines += ["  left:", *_indent(left_lines), "  right:", *_indent(right_lines)]
    if op == "==":
        # [2:] drops the two file-name lines a unified diff opens with; these sides are not files.
        diff = list(difflib.unified_diff(left_lines, right_lines, lineterm="", n=2))[2:]
        if diff:
            lines += ["  diff, - left + right:", *_indent(diff)]
    return AssertionError("\n".join(lines))


def failed_check(source, shown, value, message=None):
    """The AssertionError of a failed `assert x` or `assert not x`; `shown` is the part that decided."""
    lines = ([str(message)] if message is not None else []) + [f"assert {source}"]
    label = f"  {shown if len(shown) <= 60 else shown[:57] + '...'}:"
    rendered = _lines(value)
    if len(rendered) == 1 and len(label) + len(rendered[0]) <= _ONE_LINE:
        return AssertionError("\n".join(lines + [f"{label} {rendered[0]}"]))
    return AssertionError("\n".join(lines + [label, *_indent(rendered)]))


def _lines(value):
    """One side as text: rows the way a failed `expects` prints them, anything else as Python."""
    text = format_rows(render_rows(value)) if _is_rows(value) else pprint.pformat(value, width=_ONE_LINE)
    lines = text.splitlines() or [""]
    return lines if len(lines) <= _MAX_LINES else lines[:_MAX_LINES] + [f"... and {len(lines) - _MAX_LINES} more"]


def _is_rows(value):
    """A query result compares like a query result: a non-empty list of values or of rows of values."""
    if not isinstance(value, list) or not value:
        return False
    return all(_is_scalar(v) or (isinstance(v, (tuple, list)) and v and all(_is_scalar(x) for x in v)) for v in value)


def _is_scalar(value):
    return isinstance(value, _SCALARS) and not isinstance(value, L)


def _indent(lines, by="    "):
    return [by + line for line in lines]


# -- the rewrite --------------------------------------------------------------


def _name(id, ctx=None):
    return ast.Name(id=id, ctx=ctx or ast.Load())


def _assign(id, value):
    return ast.Assign(targets=[_name(id, ast.Store())], value=value)


def _helper(function, args):
    return ast.Call(func=ast.Attribute(value=_name(_HELPER), attr=function, ctx=ast.Load()), args=args, keywords=[])


def _guard(keep, failure):
    return ast.If(test=ast.UnaryOp(op=ast.Not(), operand=keep), body=[ast.Raise(exc=failure, cause=None)], orelse=[])


class _Rewriter(ast.NodeTransformer):
    def __init__(self, source):
        self._source = source

    def _text(self, node):
        """What the author wrote, on one line; `unparse` when the segment cannot be cut out."""
        text = ast.get_source_segment(self._source, node) or ast.unparse(node)
        return " ".join(text.split())

    def visit_Assert(self, node):
        self.generic_visit(node)
        source, test = self._text(node.test), node.test
        message = node.msg if node.msg is not None else ast.Constant(value=None)
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in _OPS:
            op = _OPS[type(test.ops[0])]
            statements = [
                _assign(_LEFT, test.left),
                _assign(_RIGHT, test.comparators[0]),
                _guard(
                    ast.Compare(left=_name(_LEFT), ops=[test.ops[0]], comparators=[_name(_RIGHT)]),
                    _helper(
                        "failed_comparison",
                        [ast.Constant(source), ast.Constant(op), _name(_LEFT), _name(_RIGHT), message],
                    ),
                ),
            ]
        else:
            negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            shown = test.operand if negated else test
            keep = ast.UnaryOp(op=ast.Not(), operand=_name(_VALUE)) if negated else _name(_VALUE)
            statements = [
                _assign(_VALUE, shown),
                _guard(
                    keep,
                    _helper(
                        "failed_check", [ast.Constant(source), ast.Constant(self._text(shown)), _name(_VALUE), message]
                    ),
                ),
            ]
        for statement in statements:
            ast.fix_missing_locations(ast.copy_location(statement, node))
        return statements


_SQL_VERBS = ("setup", "oks", "errs", "expects", "expects_contains", "exec", "teardown")


def refuse_fstring_sql(tree, path):
    """A UsageError at the first call of a verb whose SQL is written as an f-string. A string built
    on a line of its own and passed by name is the author's choice, and is not refused."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _SQL_VERBS):
            continue
        sql = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "sql"), None)
        if not isinstance(sql, ast.JoinedStr):
            continue
        named = [v.value for v in sql.values if isinstance(v, ast.FormattedValue)]
        name = next(
            (n.id if isinstance(n, ast.Name) else n.attr for n in named if isinstance(n, (ast.Name, ast.Attribute))),
            "n",
        )
        verb = node.func.attr
        raise UsageError(
            f"{os.path.relpath(path)}:{node.lineno}: SQL given to {verb}() is an f-string; write the name as "
            f"{{{name}}} and set it with ctx.env.set({name}=...), a rule's values=, or {verb}(..., {name}=...). "
            "Templates are filled per engine."
        )


def rewrite(tree, source, path=None):
    """Rewrite every `assert` of one parsed module, in place; with `path`, refuse f-string SQL first."""
    if path is not None:
        refuse_fstring_sql(tree, path)
    _Rewriter(source).visit(tree)
    at = 0
    for i, node in enumerate(tree.body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            at = 1
        elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
            at = i + 1
    tree.body.insert(at, ast.Import(names=[ast.alias(name=__name__, asname=_HELPER)]))
    return ast.fix_missing_locations(tree)


class _Loader(importlib.machinery.SourceFileLoader):
    """No bytecode cache: a `.pyc` of rewritten code would be read back by a plain import too."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        source = importlib.util.decode_source(data)
        return compile(rewrite(ast.parse(source, filename=path), source, path), path, "exec", dont_inherit=True)


def loader(name, path):
    """The import loader for one test file."""
    return _Loader(name, path)


class _Finder:
    def __init__(self, root):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path, target)
        origin = getattr(spec, "origin", None) or ""
        if not origin.endswith(".py") or not origin.startswith(self.root + os.sep):
            return None
        if origin.startswith(os.path.dirname(os.path.abspath(__file__)) + os.sep):
            return None  # a vendored copy of this package under the test tree: it would import itself
        spec.loader = _Loader(name, origin)
        return spec


def rewrite_under(root):
    """Give every module imported from inside `root` the assert texts as well."""
    root = os.path.abspath(root)
    if any(getattr(f, "root", None) == root for f in sys.meta_path):
        return
    # Before the path finder, so that nothing shadows the builtin and frozen modules ahead of it.
    before = next((i for i, f in enumerate(sys.meta_path) if f is importlib.machinery.PathFinder), len(sys.meta_path))
    sys.meta_path.insert(before, _Finder(root))
