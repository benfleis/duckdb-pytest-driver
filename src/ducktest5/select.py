"""Which cases a run takes.

    ducktest5 run PATH [PATH ...] [--select GROUP ...] [--skip GROUP ...] [--id CASE_ID ...] [--suite NAME ...]

1. TERM. `key=v1,v2` holds for a case that has any of those values for `key` (for a multi-valued key
   such as `tag`, any of the case's own values is enough). `!key=v1,v2` is the negation of the whole
   term: it holds when the case has none of those values. `!` starts a term and nowhere else --
   `key=!v1` and `key=v1,!v2` are errors that name the right spelling (`!key=v1`); the old `not:key=v`
   prefix is gone the same way, with the same hint. A term with no `key=` is a `path` term (below):
   `sql/cloud` is `path=sql/cloud`, `!sql/cloud` is `!path=sql/cloud`. Keys: `tag`, `type`, `case`,
   `path`, `suite`, and the name of any variant axis. An unknown key is an error, and so is a tag, axis
   value or path that no scanned case has -- a term that can never match is a typo.
2. GROUP. The argument of one `--select` or one `--skip` is a GROUP: one or more TERMs separated by
   `;`. A group holds for a case when ALL its terms do (AND). Whitespace around `;`, `=` and `,` is
   ignored. An empty term (`a=1;;b=2`, a trailing `;`) is an error.
3. `--select`/`--skip` may repeat; repeats OR their groups together -- a case needs only one repeated
   group to hold.
4. So selects are a disjunctive normal form (an OR of the AND-groups given), and skips are a second
   one, subtracted from what the selects give.
5. NEGATION-ONLY GROUP = SKIP. A `--select` group whose every term is negated is not a positive
   selection. By De Morgan it is translated, at parse time, into one skip group per term:
   `--select '!A;!B'` (keep cases that are neither) becomes the skips `A` and `B`. It keeps the
   default base, exactly as a lone `--skip` does. A group that mixes positive and negated terms
   (`a=1;!b=2`) stays a positive group: a=1 AND NOT b=2. Inside a `--skip` group, `!` terms are
   ordinary -- a skip is already a subtraction, so there is nothing to translate.
6. `path=GLOB[,GLOB]` matches a case's test file path, with the same glob a rule's `match()` uses:
   `*` stays inside one directory level, `**` crosses, and a pattern with no wildcard also matches
   everything below it -- `path=sql/cloud` is `path=sql/cloud/**`. Like a directory PATH, it does not
   bring in a slow or coverage case. On the command line a path term is relative to the working
   directory, exactly like a positional PATH; inside a project file (a suite's own entries) it is
   relative to the project root. Both are normalised, at parse time, to the root-relative form a case
   id uses, so matching only ever sees one form; a path that normalises outside the project root is an
   error.
7. SCOPE. Positional PATHs are a union, as in every other tool: a file, a directory, or a glob,
   relative to the working directory. They set the scope; the select groups then filter within it:

       selected = scope(PATHs, or everything if none)
                  AND (OR of the positive select groups, or everything if none)
                  MINUS (OR of the skip groups)

8. BASE. With no positive selection at all -- no PATH, no positive `--select` group, no `--suite`, no
   `--id` -- the base is the project's suite `default` if it has one, else every case whose test is
   `in_default_set`. Skips never change the base.
9. `--id` names one case exactly, for a re-run. Its path part is relative to the project root, so it
   reads and works the same from every directory. Quote it in a shell. A case named by `--id` always
   runs: no `--select`, no `--skip`, no tag and no PATH takes it out. With `--id` alone, only the named
   cases run; with PATHs, `--select` or `--suite` as well, the named cases are added to what those give
   (a union).
10. `in_default_set=False` takes a test out of that last run only: the run with nothing given, in a
    project that has no suite `default`. With a suite `default`, or with any PATH, `--select`, `--id` or
    `--suite`, it has no effect.
11. A case with the tag `slow` or `coverage` (a `.test_slow`, a `.test_coverage`) runs only when asked
    for: `--slow` (these too), `--select tag=slow` (only these), its `--id`, or its file named exactly
    as a PATH. A directory, a glob or a `path=` term does not bring it in. `--skip tag=slow` is legal
    and harmless -- these cases are already out.
12. SUITE. A suite is a named selection of cases; a case can be in any number of suites. `SUITES` in the
    project file gives each one as `{"paths": [...], "select": [...], "skip": [...]}`, with the same
    GROUP grammar as the command line: the suite holds for a case that is in scope(its paths, or
    everything) AND (OR of its select groups, or everything) MINUS (OR of its skip groups). `--suite
    NAME` (repeatable, a union) is exactly `--select suite=NAME`: `suite=NAME[,NAME2]` is an ordinary
    TERM, and `!suite=NAME` negates it -- usable on the command line and inside another suite's own
    select/skip. A suite that refers to itself, directly or through another suite, is a usage error that
    prints the cycle. Like `path=`, `suite=` alone does not bring in a slow or coverage case; a suite
    brings one in only when its own select asks for it (`tag=slow`), the same as a command line would.
13. `select --why` names the reason a case is missing: which skip removed it and where that skip came
    from -- `--skip backend=oss;storage=s3` for a command-line skip, `skip tag=spark  <- suite 'default'`
    for a suite's skip, and `--skip tag=spark (from --select !tag=spark)` for a translated negation-only
    group. A case that matches no positive `--select` group says so once, not once per group. A case
    kept or dropped because of a `suite=` term names the term as written, never the suite's definition.

Examples:

    --select 'type=python;storage=s3'                      python cases on s3
    --select type=python --select storage=s3               python cases, and all s3 cases
    --skip 'backend=oss;storage=s3'                         drop only what is both
    --select '!tag=spark'                                   the default run without spark cases (same as --skip tag=spark)
    --select 'suite=smoke;!suite=databricks'                smoke cases that are not in databricks
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .binary import UsageError
from .conf import _glob


def _is_negation_only(expr):
    return all(t.strip().startswith("!") for t in expr.split(";"))


@dataclass
class Selection:
    paths: list = field(default_factory=list)
    select: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    suites: list = field(default_factory=list)
    slow: bool = False
    skip: list = field(default_factory=list)

    def is_bare(self):
        positive_select = any(not _is_negation_only(e) for e in self.select)
        return not (self.paths or positive_select or self.ids or self.suites)


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


def _split_group_terms(expr, flag):
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
        unknown = [v for v in values if v not in ctx.suites]
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


def _parse_group(expr, flag, origin, ctx):
    return [_parse_term(t, flag, origin, ctx) for t in _split_group_terms(expr, flag)]


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


def _group_holds(case, terms, ctx):
    return all(_matches(case, key, values, ctx) != negated for negated, key, values in terms)


def _skip_reason(body, origin, from_select=None):
    where = f"--skip {body}" if origin is None else f"skip {body}  <- suite {origin!r}"
    if from_select is None:
        return where
    how = "--select" if origin is None else "select"
    return f"{where} (from {how} {from_select})"


def _classify_select_exprs(exprs, origin, ctx):
    """Splits `--select`/a suite's `select` groups into positive groups (OR'd) and the skip groups a
    negation-only group becomes -- by De Morgan, one skip group per term."""
    positive, skips = [], []
    for expr in exprs:
        terms = _parse_group(expr, "--select", origin, ctx)
        if all(negated for negated, _, _ in terms):
            for negated, key, values in terms:
                spelling = f"{key}={','.join(values)}"
                skips.append(([(False, key, values)], _skip_reason(spelling, origin, from_select=expr)))
        else:
            positive.append(terms)
    return positive, skips


def _classify_skip_exprs(exprs, origin, ctx):
    return [(_parse_group(expr, "--skip", origin, ctx), _skip_reason(expr, origin)) for expr in exprs]


def _suite_selection(name, ctx, stack=()):
    """Parses suite `name`'s own select/skip once per run. A `suite=` reference that leads back to
    `name` is a cycle, caught here -- before any case is matched -- rather than by recursing forever."""
    if name in ctx.suite_defs:
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
    """The cases suite `name` selects: scope(its paths) AND (its select) MINUS (its skip). Cached, so a
    suite is matched against every case once per run, not once per case per `suite=` term."""
    if name not in ctx.suite_members:
        paths, positive_groups, skip_groups = _suite_selection(name, ctx)
        ctx.suite_members[name] = {
            case.id
            for case in ctx.cases
            if (not paths or any(_path_matches(case.test.path, p) for p in paths))
            and (not positive_groups or any(_group_holds(case, terms, ctx) for terms in positive_groups))
            and not any(_group_holds(case, group, ctx) for group, _ in skip_groups)
        }
    return ctx.suite_members[name]


def _suite_asks_for_tag(name, tag, ctx):
    """True when suite `name`, directly or through a suite it refers to, positively asks for `tag` --
    the slow/coverage rule then treats membership in it the way `--select tag=slow` would."""
    _, positive_groups, _ = _suite_selection(name, ctx)
    for terms in positive_groups:
        for negated, key, values in terms:
            if negated:
                continue
            if key == "tag" and tag in values:
                return True
            if key == "suite" and any(_suite_asks_for_tag(v, tag, ctx) for v in values):
                return True
    return False


def apply(cases, selection, project, working_dir=None):
    """Returns (selected, why_not) where why_not maps a case id to the reason it was left out.
    `working_dir` anchors command-line `path=`/bare-path terms; it defaults to the project root."""
    axes = {axis: set(values) for axis, values in project.axes.items()}
    known_tags = {t for c in cases for t in c.tags}
    known_cases = {c.case_id for c in cases if c.case_id is not None}
    ctx = _Ctx(axes, known_tags, known_cases, cases, project.root, working_dir or project.root, project.suites)

    paths = list(selection.paths)
    # `--suite NAME` is exactly `--select suite=NAME`: one code path parses and matches it, whichever
    # spelling was used.
    select_exprs = list(selection.select) + [f"suite={name}" for name in selection.suites]
    positive_groups, skip_groups = _classify_select_exprs(select_exprs, None, ctx)
    skip_groups += _classify_skip_exprs(selection.skip, None, ctx)

    if selection.is_bare() and "default" in project.suites:
        default_suite = project.suites["default"]
        paths += default_suite.get("paths", [])
        default_positive, default_skips = _classify_select_exprs(default_suite.get("select", []), "default", ctx)
        positive_groups += default_positive
        skip_groups += default_skips
        skip_groups += _classify_skip_exprs(default_suite.get("skip", []), "default", ctx)

    # Resolve every `suite=` term named so far -- and so any cycle among them -- before any case is
    # matched.
    for terms in positive_groups + [group for group, _ in skip_groups]:
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

    bare = selection.is_bare() and "default" not in project.suites
    only_ids = bool(selection.ids) and not (paths or positive_groups)
    asked_for = {
        v for terms in positive_groups for negated, key, values in terms if key == "tag" and not negated for v in values
    }
    # A suite brings in a slow/coverage case only when its own select asks for the tag, the way a
    # command line would -- membership in the suite alone does not.
    asked_for |= {
        t
        for terms in positive_groups
        for negated, key, values in terms
        if key == "suite" and not negated
        for v in values
        for t in ("slow", "coverage")
        if _suite_asks_for_tag(v, t, ctx)
    }
    selected, why_not = [], {}
    for case in cases:
        if case.id in selection.ids:
            selected.append(case)
        elif only_ids:
            why_not[case.id] = "not named by --id"
        elif paths and not any(_path_matches(case.test.path, p) for p in paths):
            why_not[case.id] = "outside the given paths"
        elif positive_groups and not any(_group_holds(case, terms, ctx) for terms in positive_groups):
            why_not[case.id] = "matches no --select group"
        elif bare and not case.test.in_default_set:
            why_not[case.id] = "its test is not in the default set (in_default_set=False)"
        elif (
            not selection.slow
            and case.test.body_file not in paths  # a slow file named exactly is asked for
            and (slow := next((t for t in ("slow", "coverage") if t in case.tags and t not in asked_for), None))
        ):
            why_not[case.id] = f"tag {slow}: runs only when asked for: --slow, --select tag={slow}, or --id"
        elif (rule := next((r for r in skip_groups if _group_holds(case, r[0], ctx)), None)) is not None:
            why_not[case.id] = rule[1]
        else:
            selected.append(case)
    return selected, why_not
