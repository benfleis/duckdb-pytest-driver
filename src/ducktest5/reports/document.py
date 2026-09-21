"""One document of a run, as YAML and as JSON.

`05-report.yaml` and `05-report.json` are the same dictionary through two dumpers, so the two files
can never say different things. It holds nothing that is not in `04-events.jsonl`; it is the event
stream turned the way a reader asks about it: the run, the counts, and the cases in the order they
ended. `--format json` on standard output is the event lines themselves, unchanged.
"""

from __future__ import annotations

import json

import yaml

_BOOKKEEPING = ("event",)


def _case(case, *, merged):
    out = {k: v for k, v in case.items() if k not in _BOOKKEEPING}
    if not merged:
        out.pop("run", None)
    return out


def build(report):
    merged = report.merged
    return {
        "run_id": report.run_id,
        "runs": [{k: v for k, v in run.items() if k not in _BOOKKEEPING} for run in report.runs],
        "counts": report.counts,
        "seconds": report.seconds,
        "exit_code": report.exit_code,
        "not_provisioned": report.not_provisioned,
        "services": [{k: v for k, v in s.items() if k not in _BOOKKEEPING} for s in report.services],
        "cases": [_case(c, merged=merged) for c in report.cases],
    }


def write_yaml(report, path):
    with open(path, "w") as f:
        yaml.safe_dump(build(report), f, sort_keys=False, width=100)


def write_json(report, path):
    with open(path, "w") as f:
        json.dump(build(report), f, indent=1, default=str)
        f.write("\n")


def render_lines(report, out):
    """The events as they are, one JSON object per line."""
    for event in report.events:
        print(json.dumps(event, default=str), file=out)
