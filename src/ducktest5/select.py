"""Which cases a run takes.

    ducktest5 run PATH [PATH ...] [--select CLAUSE ...] [--skip CLAUSE ...] [--id CASE_ID ...] [--suite NAME ...]

1. TERM. `key=v1,v2` holds for a case that has any of those values for `key` (for a multi-valued key
   such as `tag`, any of the case's own values is enough). `!key=v1,v2` is the negation of the whole
   term: it holds when the case has none of those values. `!` starts a term and nowhere else --
   `key=!v1` and `key=v1,!v2` are errors that name the right spelling (`!key=v1`); the old `not:key=v`
   prefix is gone the same way, with the same hint. A term with no `key=` is a `path` term (below):
   `sql/cloud` is `path=sql/cloud`, `!sql/cloud` is `!path=sql/cloud`. Keys: `tag`, `type`, `case`,
   `path`, `suite`, and the name of any variant axis. An unknown key is an error, and so is a tag, axis
   value or path that no scanned case has -- a term that can never match is a typo.
2. CLAUSE. The argument of one `--select` or one `--skip` is a CLAUSE: one or more TERMs separated by
   `;`. A clause holds for a case when ALL its terms do (AND). Whitespace around `;`, `=` and `,` is
   ignored. An empty term (`a=1;;b=2`, a trailing `;`) is an error.
3. `--select`/`--skip` may repeat; repeats OR their clauses together -- a case needs only one repeated
   clause to hold.
4. So selects are a disjunctive normal form (an OR of the AND-clauses given), and skips are a second
   one, subtracted from what the selects give.
5. NEGATION-ONLY CLAUSE = SKIP. A `--select` clause whose every term is negated is not a positive
   selection. By De Morgan it is translated, at parse time, into one skip clause per term:
   `--select '!A;!B'` (keep cases that are neither) becomes the skips `A` and `B`. It is then exactly a
   lone `--skip A --skip B` -- see BASE below for what that does to the base. A clause that mixes
   positive and negated terms (`a=1;!b=2`) stays a positive clause: a=1 AND NOT b=2. Inside a `--skip`
   clause, `!` terms are ordinary -- a skip is already a subtraction, so there is nothing to translate.
6. `path=GLOB[,GLOB]` matches a case's test file path, with the same glob a rule's `match()` uses:
   `*` stays inside one directory level, `**` crosses, and a pattern with no wildcard also matches
   everything below it -- `path=sql/cloud` is `path=sql/cloud/**`. Like a directory PATH, a `path=`
   term brings in any slow or coverage case under it. On the command line a path term is relative to
   the working directory, exactly like a positional PATH; inside a project file (a suite's own entries)
   it is relative to the project root. Both are normalised, at parse time, to the root-relative form a
   case id uses, so matching only ever sees one form; a path that normalises outside the project root is
   an error.
7. SCOPE. Positional PATHs are a union, as in every other tool: a file, a directory, or a glob,
   relative to the working directory. They set the scope; the select clauses then filter within it:

       selected = scope(PATHs, or everything if none)
                  AND (OR of the positive select clauses, or everything if none)
                  MINUS (OR of the skip clauses)

8. BASE. The suite `all` is every scanned case, slow and coverage cases included; it always exists, and
   a project may not define a suite named `all`. The suite `default` is what a bare run takes -- no
   PATH, no `--select`, no `--skip`, no `--suite`, no `--id` -- and is the project's own suite `default`
   when it has one, else the built-in definition: select `suite=all`, skip `tag=slow,coverage`, minus
   any test whose `in_default_set` is `False`. Anything at all on the command line replaces the default
   entirely, not just narrows it -- `--skip tag=spark` alone runs everything else, slow cases included,
   because a skip on the command line already means the base is `all`, not `default`; write `--suite
   default --skip tag=spark` for the default minus spark.
9. `--id` names one case exactly, for a re-run. Its path part is relative to the project root, so it
   reads and works the same from every directory. Quote it in a shell. A case named by `--id` always
   runs: no `--select`, no `--skip`, no tag and no PATH takes it out. With `--id` alone, only the named
   cases run; with PATHs, `--select` or `--suite` as well, the named cases are added to what those give
   (a union).
10. `in_default_set=False` takes a test out of the built-in default only -- the base a bare run takes
    when the project has no suite `default`. A project's own suite `default` ignores it, like any suite;
    so does any PATH, `--select`, `--id`, `--suite`, or `--skip` on the command line.
11. SUITE. A suite is a named selection of cases; a case can be in any number of suites. `SUITES` in the
    project file gives each one as `{"paths": [...], "select": [...], "skip": [...]}`, with the same
    CLAUSE grammar as the command line: the suite holds for a case that is in scope(its paths, or
    everything) AND (OR of its select clauses, or everything) MINUS (OR of its skip clauses). `--suite
    NAME` (repeatable, a union) is exactly `--select suite=NAME`: `suite=NAME[,NAME2]` is an ordinary
    TERM, and `!suite=NAME` negates it -- usable on the command line and inside another suite's own
    select/skip, the same as `suite=all` or `suite=default` is. A suite that refers to itself, directly
    or through another suite, is a usage error that prints the cycle.
12. `select --explain` names the reason a case is missing: which skip removed it and where that skip
    came from -- `--skip backend=oss;storage=s3` for a command-line skip, `skip tag=spark  <- suite
    'default'` for a suite's skip, and `--skip tag=spark (from --select !tag=spark)` for a translated
    negation-only clause. A case that matches no positive `--select` clause says so once, not once per
    clause -- unless that whole positive selection is a single `suite=NAME` term, when it instead reads
    `not in suite 'NAME'`, the same whether the case is outside the suite or was dropped by the suite's
    own skip; a project-defined suite's definition is never expanded here. A case the built-in default
    drops adds why: `not in suite 'default': tag slow` (or `coverage`, or `in_default_set=False`), so a
    bare run explains itself.

Examples:

    --select 'type=python;storage=s3'                      python cases on s3
    --select type=python --select storage=s3               python cases, and all s3 cases
    --skip 'backend=oss;storage=s3'                         drop only what is both
    --skip tag=spark                                        everything except spark cases -- slow cases included
    --select '!tag=spark'                                   the same, by De Morgan
    --suite default --skip tag=spark                        the default run, minus spark cases
    --select 'suite=smoke;!suite=databricks'                smoke cases that are not in databricks
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .binary import UsageError
from .conf import _glob


@dataclass
class Selection:
    paths: list = field(default_factory=list)
    select: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    suites: list = field(default_factory=list)
    skip: list = field(default_factory=list)

    def is_bare(self):
        return not (self.paths or self.select or self.skip or self.suites or self.ids)


def _path_matches(case_path, wanted):
    wanted = wanted.rstrip("/")
    return (
        case_path == wanted
        or case_path.startswith(wanted + "/")
        or fnmatch.fnmatchcase(case_path, wanted)
        # `foo.test` names the paired `foo.py` too, and the other way round; nothing wider than that
        or (
            os.path.splitext(case_path)[0] == os.path.splitext(wanted)[0]
            and {os.path.splitext(case_path)[1], os.path.splitext(wanted)[1]} & {".py"}
            and {os.path.splitext(case_path)[1], os.path.splitext(wanted)[1]} != {".py"}
        )
    )


def _path_term_matches(case_root_path, pattern):
    """`path=` uses the project's own glob (conf.py's `match()`); a pattern with no wildcard also
    stands for everything below it, as if `/**` had been appended."""
    if set(pattern) & {"*", "?"}:
        return _glob(pattern).fullmatch(case_root_path) is not None
    stripped = pattern.rstrip("/")
    return case_root_path == stripped or case_root_path.startswith(stripped + "/")


def _looks_like_path(s):
    return "/" in s or any(c in "*?" for c in s)


@dataclass
class _Ctx:
    axes: dict
    known_tags: set
    known_cases: set
    cases: list
    project_root: str
    working_dir: str
    suites: dict
    # Per-run memoisation: a suite is parsed and matched against every case at most once, however many
    # `suite=` terms name it.
    suite_defs: dict = field(default_factory=dict)
    suite_members: dict = field(default_factory=dict)


def _root_relative(value, origin, ctx, flag, term):
    """A command-line path term (origin is None) is typed relative to the working directory, exactly
    like a positional PATH; a suite's own entry (origin is its name) is already relative to the
    project root. Both become root-relative here, the one form the matcher and `path=`'s validation see."""
    if origin is None:
        abs_path = os.path.normpath(os.path.join(ctx.working_dir, value))
        value = os.path.relpath(abs_path, ctx.project_root).replace(os.sep, "/")
    if value == ".." or value.startswith("../"):
        raise UsageError(f"{flag} {term!r}: path {value!r} is outside the project root")
    return value


def _split_clause_terms(expr, flag):
    terms = [t.strip() for t in expr.split(";")]
    if any(not t for t in terms):
        raise UsageError(f"{flag} {expr!r}: empty term -- terms are key=value, separated by ';'")
    return terms


def _parse_term(term, flag, origin, ctx):
    negated = term.startswith("!")
    body = term[1:].strip() if negated else term
    if body.startswith("not:"):
        raise UsageError(f"{flag} {term!r}: 'not:' is gone; write !{body[4:].strip()}")
    if not body:
        raise UsageError(f"{flag} {term!r}: write key=value[,value] or !key=value")
    key, sep, raw = body.partition("=") if "=" in body else ("path", "", body)
    key = key.strip()
    if sep and not raw.strip():
        raise UsageError(f"{flag} {term!r}: write key=value[,value] or !key=value")
    values = [v.strip() for v in raw.split(",")]
    bad = next((v for v in values if v.startswith("!")), None)
    if bad is not None:
        raise UsageError(f"{flag} {term!r}: '!' only starts a term; write !{key}={bad[1:]}")
    if key == "tag":
        unknown = [v for v in values if v not in ctx.known_tags]
        if unknown:
            raise UsageError(
                f"{flag} {term!r}: no scanned case has tag {unknown}; known tags: {sorted(ctx.known_tags)}"
            )
    elif key == "path":
        values = [_root_relative(v, origin, ctx, flag, term) for v in values]
        unknown = [v for v in values if not any(_path_term_matches(c.test.root_path, v) for c in ctx.cases)]
        if unknown:
            raise UsageError(f"{flag} {term!r}: path {unknown} matches no scanned case")
    elif key == "suite":
        unknown = [v for v in values if v not in ctx.suites and v not in ("all", "default")]
        if unknown:
            raise UsageError(f"{flag} {term!r}: no suite {unknown}; known suites: {sorted(ctx.suites)}")
    elif key in ctx.axes:
        unknown = [v for v in values if v not in ctx.axes[key]]
        if unknown:
            raise UsageError(f"{flag} {term!r}: axis {key!r} has no value {unknown}; it has {sorted(ctx.axes[key])}")
    elif key == "type":
        if unknown := [v for v in values if v not in ("sqllogic", "paired", "python")]:
            raise UsageError(f"{flag} {term!r}: no type {unknown}; types are sqllogic, paired, python")
    elif key == "case":
        if unknown := [v for v in values if v not in ctx.known_cases]:
            raise UsageError(f"{flag} {term!r}: no test has a case {unknown}")
    elif _looks_like_path(term):
        raise UsageError(f"{flag} {term!r}: a bare path can't contain '='; write path={key}")
    else:
        raise UsageError(
            f"{flag} {term!r}: unknown key {key!r}; keys are tag, type, case, path, suite"
            + "".join(f", {a}" for a in ctx.axes)
        )
    return negated, key, values


def _parse_clause(expr, flag, origin, ctx):
    return [_parse_term(t, flag, origin, ctx) for t in _split_clause_terms(expr, flag)]


def _matches(case, key, values, ctx):
    """True when `case` has any of `values` for `key` -- "holds" for a positive term, "removes" for a skip term."""
    if key == "tag":
        return any(v in case.tags for v in values)
    if key == "type":
        return case.test.type in values
    if key == "case":
        return case.case_id in values
    if key == "path":
        return any(_path_term_matches(case.test.root_path, v) for v in values)
    if key == "suite":
        return any(case.id in _suite_members(v, ctx) for v in values)
    return key in case.axis_values and case.axis_values[key].id in values


def _clause_holds(case, terms, ctx):
    return all(_matches(case, key, values, ctx) != negated for negated, key, values in terms)


def _skip_reason(body, origin, from_select=None):
    where = f"--skip {body}" if origin is None else f"skip {body}  <- suite {origin!r}"
    if from_select is None:
        return where
    how = "--select" if origin is None else "select"
    return f"{where} (from {how} {from_select})"


def _classify_select_exprs(exprs, origin, ctx):
    """Splits `--select`/a suite's `select` clauses into positive clauses (OR'd) and the skip clauses a
    negation-only clause becomes -- by De Morgan, one skip clause per term."""
    positive, skips = [], []
    for expr in exprs:
        terms = _parse_clause(expr, "--select", origin, ctx)
        if all(negated for negated, _, _ in terms):
            for negated, key, values in terms:
                spelling = f"{key}={','.join(values)}"
                skips.append(([(False, key, values)], _skip_reason(spelling, origin, from_select=expr)))
        else:
            positive.append(terms)
    return positive, skips


def _classify_skip_exprs(exprs, origin, ctx):
    return [(_parse_clause(expr, "--skip", origin, ctx), _skip_reason(expr, origin)) for expr in exprs]


def _suite_selection(name, ctx, stack=()):
    """Parses suite `name`'s own select/skip once per run -- including the built-in suites `all` (always
    every case) and `default` (when the project does not define its own). A `suite=` reference that leads
    back to `name` is a cycle, caught here -- before any case is matched -- rather than by recursing
    forever."""
    if name in ctx.suite_defs:
        return ctx.suite_defs[name]
    if name == "all":
        ctx.suite_defs[name] = ([], [], [])
        return ctx.suite_defs[name]
    if name == "default" and "default" not in ctx.suites:
        positive = [[(False, "suite", ["all"])]]
        skips = [([(False, "tag", ["slow", "coverage"])], "skip tag=slow,coverage  <- suite 'default'")]
        ctx.suite_defs[name] = ([], positive, skips)
        return ctx.suite_defs[name]
    if name in stack:
        cycle = stack[stack.index(name) :] + (name,)
        raise UsageError(f"suite cycle: {' -> '.join(cycle)}")
    suite = ctx.suites[name]
    paths = suite.get("paths", [])
    for p in paths:
        if not any(_path_matches(c.test.path, p) for c in ctx.cases):
            raise UsageError(f"suite {name!r}: path {p!r} holds no test")
    positive, skips = _classify_select_exprs(suite.get("select", []), name, ctx)
    skips += _classify_skip_exprs(suite.get("skip", []), name, ctx)
    for terms in positive + [g for g, _ in skips]:
        for _, key, values in terms:
            if key == "suite":
                for v in values:
                    _suite_selection(v, ctx, stack + (name,))
    ctx.suite_defs[name] = (paths, positive, skips)
    return ctx.suite_defs[name]


def _suite_members(name, ctx):
    """The cases suite `name` selects: scope(its paths) AND (its select) MINUS (its skip) -- and, for the
    built-in `default`, minus any test with `in_default_set=False` too, which no clause can express.
    Cached, so a suite is matched against every case once per run, not once per case per `suite=` term."""
    if name not in ctx.suite_members:
        paths, positive_clauses, skip_clauses = _suite_selection(name, ctx)
        builtin_default = name == "default" and "default" not in ctx.suites
        ctx.suite_members[name] = {
            case.id
            for case in ctx.cases
            if (not paths or any(_path_matches(case.test.path, p) for p in paths))
            and (not positive_clauses or any(_clause_holds(case, terms, ctx) for terms in positive_clauses))
            and not any(_clause_holds(case, clause, ctx) for clause, _ in skip_clauses)
            and not (builtin_default and not case.test.in_default_set)
        }
    return ctx.suite_members[name]


def _sole_suite_term(positive_clauses):
    """The values of a positive selection that is a single un-negated `suite=` term, else None."""
    if len(positive_clauses) != 1 or len(positive_clauses[0]) != 1:
        return None
    negated, key, values = positive_clauses[0][0]
    return None if negated or key != "suite" else values


def _default_exclusion_reason(case):
    if "slow" in case.tags:
        return "tag slow"
    if "coverage" in case.tags:
        return "tag coverage"
    return "in_default_set=False"


def _not_in_positive_reason(case, positive_clauses, ctx):
    """The line for a case that matches no positive `--select` clause -- generic, unless the whole
    positive selection is a single `suite=NAME` term, when it names the suite instead."""
    values = _sole_suite_term(positive_clauses)
    if values is None:
        return "matches no --select clause"
    reason = "not in suite " + " or ".join(repr(v) for v in values)
    if values == ["default"] and "default" not in ctx.suites:
        reason += f": {_default_exclusion_reason(case)}"
    return reason


def apply(cases, selection, project, working_dir=None):
    """Returns (selected, why_not) where why_not maps a case id to the reason it was left out.
    `working_dir` anchors command-line `path=`/bare-path terms; it defaults to the project root."""
    axes = {axis: set(values) for axis, values in project.axes.items()}
    known_tags = {t for c in cases for t in c.tags}
    known_cases = {c.case_id for c in cases if c.case_id is not None}
    ctx = _Ctx(axes, known_tags, known_cases, cases, project.root, working_dir or project.root, project.suites)

    if selection.is_bare():
        paths = []
        positive_clauses = [[(False, "suite", ["default"])]]
        skip_clauses = []
    else:
        paths = list(selection.paths)
        # `--suite NAME` is exactly `--select suite=NAME`: one code path parses and matches it, whichever
        # spelling was used.
        select_exprs = list(selection.select) + [f"suite={name}" for name in selection.suites]
        positive_clauses, skip_clauses = _classify_select_exprs(select_exprs, None, ctx)
        skip_clauses += _classify_skip_exprs(selection.skip, None, ctx)

    # Resolve every `suite=` term named so far -- and so any cycle among them -- before any case is
    # matched.
    for terms in positive_clauses + [clause for clause, _ in skip_clauses]:
        for _, key, values in terms:
            if key == "suite":
                for v in values:
                    _suite_selection(v, ctx)

    if selection.ids:
        known = {c.id for c in cases}
        unknown = [i for i in selection.ids if i not in known]
        if unknown:
            raise UsageError(f"--id: no such case: {unknown}")
    for p in paths:
        if not any(_path_matches(c.test.path, p) for c in cases):
            raise UsageError(f"path {p!r} holds no test")

    only_ids = bool(selection.ids) and not (paths or positive_clauses)
    selected, why_not = [], {}
    for case in cases:
        if case.id in selection.ids:
            selected.append(case)
        elif only_ids:
            why_not[case.id] = "not named by --id"
        elif paths and not any(_path_matches(case.test.path, p) for p in paths):
            why_not[case.id] = "outside the given paths"
        elif positive_clauses and not any(_clause_holds(case, terms, ctx) for terms in positive_clauses):
            why_not[case.id] = _not_in_positive_reason(case, positive_clauses, ctx)
        elif (rule := next((r for r in skip_clauses if _clause_holds(case, r[0], ctx)), None)) is not None:
            why_not[case.id] = rule[1]
        else:
            selected.append(case)
    return selected, why_not
