"""Which cases a run takes.

    ducktest5 run PATH [PATH ...] [--select EXPR ...] [--id CASE_ID ...] [--set NAME]

- PATHs are a union, as in every other tool: a file, a directory, or a glob, relative to the working
  directory.
- Every `--select EXPR` must hold. An EXPR is `key=v1,v2` (any of the values) or `not:key=v1,v2`.
  Keys: `tag`, `type`, `case`, and the name of any variant axis. An unknown key is an error, and so is
  a tag or an axis value that no scanned case has: a selection that can never match is a typo.
- `--id` names one case exactly, for a re-run. Its path part is relative to the project root, so it
  reads and works the same from every directory. Quote it in a shell. A case named by id always runs:
  no `--select`, no tag and no PATH takes it out. With `--id` alone, only the named cases run; with
  PATHs, `--select` or `--set` as well, the named cases are added to what those give (a union).
- `--set NAME` takes a named set from the project's SETS; a set is `{"paths": [...], "select": [...]}`.
- With nothing given, the set `default` applies if the project has one; else every case whose test is
  in the default set.
- `in_default_set=False` takes a test out of that last run only: the run with nothing given, in a
  project that has no set `default`. With a set `default`, or with any PATH, `--select`, `--id` or
  `--set`, it has no effect.
- A case with the tag `slow` or `coverage` (a `.test_slow`, a `.test_coverage`) runs only when asked
  for: `--slow` (these too), `--select tag=slow` (only these), its `--id`, or its file named exactly
  as a PATH. A directory or a glob does not bring it in.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .binary import UsageError


@dataclass
class Selection:
    paths: list = field(default_factory=list)
    select: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    set_name: str | None = None
    slow: bool = False

    def is_bare(self):
        return not (self.paths or self.select or self.ids or self.set_name)


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


def _parse(expr, axes, known_tags, known_cases):
    negate = expr.startswith("not:")
    body = expr[4:] if negate else expr
    key, sep, raw = body.partition("=")
    if not sep or not raw:
        raise UsageError(f"--select {expr!r}: write key=value[,value] or not:key=value")
    values = raw.split(",")
    if key == "tag":
        unknown = [v for v in values if v not in known_tags]
        if unknown:
            raise UsageError(f"--select {expr!r}: no scanned case has tag {unknown}; known tags: {sorted(known_tags)}")
    elif key in axes:
        unknown = [v for v in values if v not in axes[key]]
        if unknown:
            raise UsageError(f"--select {expr!r}: axis {key!r} has no value {unknown}; it has {sorted(axes[key])}")
    elif key == "type":
        if unknown := [v for v in values if v not in ("sqllogic", "paired", "python")]:
            raise UsageError(f"--select {expr!r}: no type {unknown}; types are sqllogic, paired, python")
    elif key == "case":
        if unknown := [v for v in values if v not in known_cases]:
            raise UsageError(f"--select {expr!r}: no test has a case {unknown}")
    else:
        raise UsageError(
            f"--select {expr!r}: unknown key {key!r}; keys are tag, type, case" + "".join(f", {a}" for a in axes)
        )
    return negate, key, values


def _holds(case, negate, key, values):
    if key == "tag":
        hit = any(v in case.tags for v in values)
    elif key == "type":
        hit = case.test.type in values
    elif key == "case":
        hit = case.case_id in values
    else:
        hit = key in case.axis_values and case.axis_values[key].id in values
    return hit != negate


def apply(cases, selection, project):
    """Returns (selected, why_not) where why_not maps a case id to the reason it was left out."""
    axes = {axis: set(values) for axis, values in project.axes.items()}
    known_tags = {t for c in cases for t in c.tags}
    paths, exprs = list(selection.paths), list(selection.select)
    if selection.set_name:
        named = project.sets.get(selection.set_name)
        if named is None:
            raise UsageError(f"--set {selection.set_name!r}: the project has sets {sorted(project.sets)}")
        paths += named.get("paths", [])
        exprs += named.get("select", [])
    elif selection.is_bare() and "default" in project.sets:
        paths += project.sets["default"].get("paths", [])
        exprs += project.sets["default"].get("select", [])
    known_cases = {c.case_id for c in cases if c.case_id is not None}
    parsed = [_parse(e, axes, known_tags, known_cases) for e in exprs]

    if selection.ids:
        known = {c.id for c in cases}
        unknown = [i for i in selection.ids if i not in known]
        if unknown:
            raise UsageError(f"--id: no such case: {unknown}")
    for p in paths:
        if not any(_path_matches(c.test.path, p) for c in cases):
            raise UsageError(f"path {p!r} holds no test")

    bare = selection.is_bare() and "default" not in project.sets
    only_ids = bool(selection.ids) and not (paths or exprs)
    asked_for = {v for negate, key, values in parsed if key == "tag" and not negate for v in values}
    selected, why_not = [], {}
    for case in cases:
        if case.id in selection.ids:
            selected.append(case)
        elif only_ids:
            why_not[case.id] = "not named by --id"
        elif paths and not any(_path_matches(case.test.path, p) for p in paths):
            why_not[case.id] = "outside the given paths"
        elif (failed := next((e for e, p in zip(exprs, parsed) if not _holds(case, *p)), None)) is not None:
            why_not[case.id] = f"--select {failed}"
        elif bare and not case.test.in_default_set:
            why_not[case.id] = "its test is not in the default set (in_default_set=False)"
        elif (
            not selection.slow
            and case.test.body_file not in paths  # a slow file named exactly is asked for
            and (slow := next((t for t in ("slow", "coverage") if t in case.tags and t not in asked_for), None))
        ):
            why_not[case.id] = f"tag {slow}: runs only when asked for: --slow, --select tag={slow}, or --id"
        else:
            selected.append(case)
    return selected, why_not
