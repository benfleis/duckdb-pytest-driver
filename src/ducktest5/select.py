"""Which cases a run takes.

    ducktest5 run PATH [PATH ...] [--select EXPR ...] [--skip EXPR ...] [--id CASE_ID ...] [--set NAME]

Selects pick a base; skips always come after. That is why there are two words: a selection decides
what set of cases you start from, a skip only ever removes from it -- a lone `--skip` still runs the
default set, minus what it names.

1. POSITIVE selection sets the base: PATHs, `--select key=v1,v2` (not negated), and `--set NAME`.
   PATHs are a union, as in every other tool: a file, a directory, or a glob, relative to the working
   directory. Every positive `--select` must hold (AND). Keys: `tag`, `type`, `case`, and the name of
   any variant axis; an unknown key is an error, and so is a tag or an axis value that no scanned case
   has: a selection that can never match is a typo. A set contributes its own paths and selects, the
   same as a PATH or `--select` given on the command line. With no positive selection at all, the base
   is the project's set `default` if it has one, else every case whose test is `in_default_set`.
2. SKIPS remove cases from the base; they never change it. `--skip EXPR` (repeatable) uses the same
   `key=v1,v2` syntax and validation as `--select`. A case is removed if ANY skip matches it (for `tag`
   and other multi-valued keys: if the case has any of the listed values).
3. `--select not:key=v1,v2` and `--select !key=v1,v2` are translated, at parse time, into exactly
   `--skip key=v1,v2` -- they are not a positive selection (rule 1), and after translation nothing else
   knows they were ever a negation. `--skip not:...` or `--skip !...` is an error: a skip is already a
   negation.
4. A named set (`SETS` in the project file) is `{"paths": [...], "select": [...], "skip": [...]}`. Its
   skips apply in addition to any given on the command line. A negated entry inside its `select` list
   translates the same way as on the command line. The set `default` may have skips too.
5. `--id` names one case exactly, for a re-run. Its path part is relative to the project root, so it
   reads and works the same from every directory. Quote it in a shell. A case named by `--id` always
   runs: no `--select`, no `--skip`, no tag and no PATH takes it out. With `--id` alone, only the named
   cases run; with PATHs, `--select` or `--set` as well, the named cases are added to what those give
   (a union).
6. `in_default_set=False` takes a test out of that last run only: the run with nothing given, in a
   project that has no set `default`. With a set `default`, or with any PATH, `--select`, `--id` or
   `--set`, it has no effect.
7. A case with the tag `slow` or `coverage` (a `.test_slow`, a `.test_coverage`) runs only when asked
   for: `--slow` (these too), `--select tag=slow` (only these), its `--id`, or its file named exactly
   as a PATH. A directory or a glob does not bring it in. `--skip tag=slow` is legal and harmless --
   these cases are already out.
8. `select --why` names the reason a case is missing: which `--select` failed, or which skip removed
   it and where that skip came from, e.g. `--skip tag=spark` for a command-line `--skip`,
   `skip tag=spark  <- set 'default'` for a set's skip, and `--skip tag=spark (from --select
   not:tag=spark)` for a translated negation.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .binary import UsageError


def _is_negated(expr):
    return expr.startswith("not:") or expr.startswith("!")


@dataclass
class Selection:
    paths: list = field(default_factory=list)
    select: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    set_name: str | None = None
    slow: bool = False
    skip: list = field(default_factory=list)

    def is_bare(self):
        positive_select = any(not _is_negated(e) for e in self.select)
        return not (self.paths or positive_select or self.ids or self.set_name)


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


def _split_negation(expr):
    if expr.startswith("not:"):
        return True, expr[4:]
    if expr.startswith("!"):
        return True, expr[1:]
    return False, expr


def _parse_kv(expr, body, flag, axes, known_tags, known_cases):
    """`body` is `expr` with any not:/! prefix already stripped."""
    key, sep, raw = body.partition("=")
    if not sep or not raw:
        raise UsageError(f"{flag} {expr!r}: write key=value[,value] or not:key=value")
    values = raw.split(",")
    if key == "tag":
        unknown = [v for v in values if v not in known_tags]
        if unknown:
            raise UsageError(f"{flag} {expr!r}: no scanned case has tag {unknown}; known tags: {sorted(known_tags)}")
    elif key in axes:
        unknown = [v for v in values if v not in axes[key]]
        if unknown:
            raise UsageError(f"{flag} {expr!r}: axis {key!r} has no value {unknown}; it has {sorted(axes[key])}")
    elif key == "type":
        if unknown := [v for v in values if v not in ("sqllogic", "paired", "python")]:
            raise UsageError(f"{flag} {expr!r}: no type {unknown}; types are sqllogic, paired, python")
    elif key == "case":
        if unknown := [v for v in values if v not in known_cases]:
            raise UsageError(f"{flag} {expr!r}: no test has a case {unknown}")
    else:
        raise UsageError(
            f"{flag} {expr!r}: unknown key {key!r}; keys are tag, type, case" + "".join(f", {a}" for a in axes)
        )
    return key, values


def _matches(case, key, values):
    """True if `case` has any of `values` for `key` -- "must hold" for a select, "removes" for a skip."""
    if key == "tag":
        return any(v in case.tags for v in values)
    if key == "type":
        return case.test.type in values
    if key == "case":
        return case.case_id in values
    return key in case.axis_values and case.axis_values[key].id in values


def _skip_reason(body, origin, from_select=None):
    where = f"--skip {body}" if origin is None else f"skip {body}  <- set {origin!r}"
    if from_select is None:
        return where
    how = "--select" if origin is None else "select"
    return f"{where} (from {how} {from_select})"


def _split_selects(exprs, origin, axes, known_tags, known_cases):
    """Splits `--select`/a set's `select` EXPRs into positive filters and the skips a negation becomes."""
    positive, skips = [], []
    for expr in exprs:
        negated, body = _split_negation(expr)
        key, values = _parse_kv(expr, body, "--select", axes, known_tags, known_cases)
        if negated:
            skips.append((key, values, _skip_reason(body, origin, from_select=expr)))
        else:
            positive.append((expr, key, values))
    return positive, skips


def _split_skips(exprs, origin, axes, known_tags, known_cases):
    """Parses `--skip`/a set's `skip` EXPRs; a skip may not itself be a negation."""
    skips = []
    for expr in exprs:
        negated, body = _split_negation(expr)
        if negated:
            raise UsageError(f"--skip {expr!r}: a skip is already a negation; write --skip {body}")
        key, values = _parse_kv(expr, body, "--skip", axes, known_tags, known_cases)
        skips.append((key, values, _skip_reason(expr, origin)))
    return skips


def apply(cases, selection, project):
    """Returns (selected, why_not) where why_not maps a case id to the reason it was left out."""
    axes = {axis: set(values) for axis, values in project.axes.items()}
    known_tags = {t for c in cases for t in c.tags}
    known_cases = {c.case_id for c in cases if c.case_id is not None}

    paths = list(selection.paths)
    positive, skip_rules = _split_selects(selection.select, None, axes, known_tags, known_cases)
    skip_rules += _split_skips(selection.skip, None, axes, known_tags, known_cases)

    if selection.set_name:
        named = project.sets.get(selection.set_name)
        if named is None:
            raise UsageError(f"--set {selection.set_name!r}: the project has sets {sorted(project.sets)}")
        paths += named.get("paths", [])
        set_positive, set_skips = _split_selects(
            named.get("select", []), selection.set_name, axes, known_tags, known_cases
        )
        positive += set_positive
        skip_rules += set_skips
        skip_rules += _split_skips(named.get("skip", []), selection.set_name, axes, known_tags, known_cases)
    elif selection.is_bare() and "default" in project.sets:
        default_set = project.sets["default"]
        paths += default_set.get("paths", [])
        default_positive, default_skips = _split_selects(
            default_set.get("select", []), "default", axes, known_tags, known_cases
        )
        positive += default_positive
        skip_rules += default_skips
        skip_rules += _split_skips(default_set.get("skip", []), "default", axes, known_tags, known_cases)

    if selection.ids:
        known = {c.id for c in cases}
        unknown = [i for i in selection.ids if i not in known]
        if unknown:
            raise UsageError(f"--id: no such case: {unknown}")
    for p in paths:
        if not any(_path_matches(c.test.path, p) for c in cases):
            raise UsageError(f"path {p!r} holds no test")

    bare = selection.is_bare() and "default" not in project.sets
    only_ids = bool(selection.ids) and not (paths or positive)
    asked_for = {v for _, key, values in positive if key == "tag" for v in values}
    selected, why_not = [], {}
    for case in cases:
        if case.id in selection.ids:
            selected.append(case)
        elif only_ids:
            why_not[case.id] = "not named by --id"
        elif paths and not any(_path_matches(case.test.path, p) for p in paths):
            why_not[case.id] = "outside the given paths"
        elif (
            failed := next((expr for expr, key, values in positive if not _matches(case, key, values)), None)
        ) is not None:
            why_not[case.id] = f"--select {failed}"
        elif bare and not case.test.in_default_set:
            why_not[case.id] = "its test is not in the default set (in_default_set=False)"
        elif (
            not selection.slow
            and case.test.body_file not in paths  # a slow file named exactly is asked for
            and (slow := next((t for t in ("slow", "coverage") if t in case.tags and t not in asked_for), None))
        ):
            why_not[case.id] = f"tag {slow}: runs only when asked for: --slow, --select tag={slow}, or --id"
        elif (rule := next((r for r in skip_rules if _matches(case, r[0], r[1])), None)) is not None:
            why_not[case.id] = rule[2]
        else:
            selected.append(case)
    return selected, why_not
