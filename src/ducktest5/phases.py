"""The phases, as files in one run directory.

    01-tests.yaml      scan       the cases a selection gives, and what each one needs
    02-plan.yaml       plan       the batches, the runner of each, and the knobs used
    03-provision.yaml  provision  the services that are up, secrets redacted
    04-events.jsonl    exec       one line for each case attempt (`events.py`)
    05-report.*        report     what a reader gets (`reports/`)

Each phase is a subcommand over one `--run-dir`: it reads the files of the phases before it and
writes its own. `ducktest5 run` is the same functions in one process, in that order, so there is one
implementation of each phase and not two.

What crosses an invocation boundary:

- `01` and `02` need no infrastructure and hold no secret. Every path in them is relative to the
  project root, so they can be made on one machine and carried to another; the binaries, which are
  of a machine, are named by `exec` and are not in them.
- `03` holds redacted blocks only. A secret is never written down: `exec` in a new process resolves
  each service again, an `existing` one from the same source (`--existing-service`, the environment)
  and a kept one from the `attach` line `03` records.
- A service that `provision` may leave running is one that says `detached=True`: it is not in the
  process that starts it (a container, a cloud workspace). One that does not can only be provisioned
  by the process that uses it, which is `exec --provision` and `run`.
- Engines (Spark, the DuckDB shell) always belong to the exec process.

A phase that finds a file of a schema it does not know stops and says so. A phase that finds its own
output already there refuses, unless `--force` (`exec`: `--resume`, `--only-failed`).
"""

from __future__ import annotations

import hashlib
import os
import shutil

import yaml

from . import caseenv, events, sqllogic
from .binary import UsageError
from .model import SQLLOGIC
from .outcomes import BLOCKED, ERROR, FAILED, PASSED
from .provision import MASK, attach_overrides, existing_overrides, provision, release

TESTS_FILE, PLAN_FILE, PROVISION_FILE = "01-tests.yaml", "02-plan.yaml", "03-provision.yaml"
TESTS_SCHEMA = PLAN_SCHEMA = PROVISION_SCHEMA = 1

PYTHON_RUNNER, UNITTEST_RUNNER, RUN_RUNNER = "python", "unittest", "run"
RUNNERS = (UNITTEST_RUNNER, RUN_RUNNER)  # what `--runner` may say; a Python body is always `python`

KEPT_OUTCOMES = (FAILED, ERROR, BLOCKED)  # what a case must end with for its files to stay


def run_id_of(run_dir):
    """The run id is the name of its directory, so a phase in a new process knows it from the path."""
    return os.path.basename(os.path.abspath(run_dir.rstrip(os.sep)))


def _dump(path, doc):
    with open(path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, width=100)


def write(run_dir, name, doc, *, force=False):
    path = os.path.join(run_dir, name)
    if os.path.exists(path) and not force:
        raise UsageError(f"{path} is already there: that phase ran for this run directory. Use --force to do it again.")
    os.makedirs(run_dir, exist_ok=True)
    _dump(path, doc)
    return path


def read(run_dir, name, schema, *, made_by):
    path = run_dir if os.path.isfile(run_dir) else os.path.join(run_dir, name)
    if not os.path.isfile(path):
        raise UsageError(f"no {name} in {run_dir}: run `ducktest5 {made_by} --run-dir {run_dir}` first")
    doc = yaml.safe_load(open(path)) or {}
    found = doc.get("schema")
    if found != schema:
        newer = "a newer ducktest5 wrote it" if isinstance(found, int) and found > schema else "it is not one of ours"
        raise UsageError(f"{path} says schema {found!r} and this ducktest5 knows {schema}: {newer}.")
    return doc


# -- 01: the cases ------------------------------------------------------------


def write_tests(run_dir, cases, project, *, working_dir=".", selection=None, force=False):
    """`01-tests.yaml`: every selected case with its case environment as far as it is known with no
    service running. Fields: `schema`, `made_at`, `project` (the root's name), `working_dir` (where
    the paths were read from, relative to the project root), `selection` (what was asked for), and
    `tests`, one record for each case: `identity` (id, test, type, file, case, variants),
    `selection` (tags, and the suites the test declares itself in or out of), `preconditions`
    (services, engines, mutex), `runtime` (case params, environment,
    timeout, retries) and `why` (the rules that gave the test what it has)."""
    doc = {
        "schema": TESTS_SCHEMA,
        "made_at": events.now(),
        "project": os.path.basename(os.path.abspath(project.root)),
        "working_dir": os.path.relpath(os.path.abspath(working_dir), project.root).replace(os.sep, "/"),
        "selection": selection or {},
        "tests": [c.record(project) for c in cases],
    }
    return write(run_dir, TESTS_FILE, doc, force=force)


def read_tests(run_dir):
    return read(run_dir, TESTS_FILE, TESTS_SCHEMA, made_by="scan")


def ids_of_tests(doc):
    return [t["identity"]["id"] for t in doc.get("tests") or []]


# -- 02: the batches ----------------------------------------------------------


def _alone(case):
    """A slow file takes a process for itself; a case with a service runs alone, so that a failure
    that runs again for its text does not see what the first attempt left on the service. A case
    that names a mutex takes its batch with it into the lock, so it is one case."""
    return "slow" in case.tags or bool(case.services) or bool(case.mutex)


def _chunks(group, batch_size):
    """The cases of one environment, in batches of at most `batch_size`. A file is in a batch once:
    the binary reports by file name, so two cases of one file could not be told apart."""
    chunks, alone = [], [[c] for c in group if _alone(c)]
    for c in (c for c in group if not _alone(c)):
        chunk = next(
            (k for k in chunks if len(k) < batch_size and c.test.body_file not in {x.test.body_file for x in k}), None
        )
        if chunk is None:
            chunks.append(chunk := [])
        chunk.append(c)
    return chunks + alone


def _key_record(case_env):
    """What makes two batches different, for a reader: a short digest of the batch key and the case
    environment that key is made of, secrets masked."""
    key = case_env.key()
    return {"digest": hashlib.sha1(key.encode()).hexdigest()[:12], "environment": case_env.record()}


def build_plan(cases, *, batch_size=10, workers=4, runner=UNITTEST_RUNNER, run_args="", keep=False):
    """`02-plan.yaml` as a document. `.test` cases with one case environment share a batch; a Python
    body is a batch of one, whatever `--runner` says, because only this process can run it.

    A batch: `id`, `runner`, `order`, `cases` (the case ids), `tests` (the `.test` files, relative to
    the project root), `timeout` and `timeouts`/`retries` where a case has its own, `mutex` where a
    case names one, and `key` (the digest and the case environment the batch is made of). `knobs`
    records what the planner was given; the batch key is worked out again before a batch runs, so a
    plan can be read by another engine without being believed."""
    if runner not in RUNNERS:
        raise UsageError(f"--runner {runner!r}; it is one of {RUNNERS}")
    batches, groups = [], {}
    for case in (c for c in cases if c.test.type == SQLLOGIC):
        groups.setdefault(caseenv.resolve(case).key(), []).append(case)
    for group in groups.values():
        for chunk in _chunks(group, batch_size):
            batches.append(
                {
                    "id": f"b{len(batches) + 1:03d}",
                    "runner": runner,
                    "order": len(batches) + 1,
                    "cases": [c.id for c in chunk],
                    "tests": [c.test.root_path for c in chunk],
                    "timeout": sqllogic.DEFAULT_TIMEOUT_S,
                    "timeouts": {c.id: c.test.timeout for c in chunk if c.test.timeout},
                    "retries": {c.id: c.test.retries for c in chunk if c.test.retries},
                    **({"mutex": sorted({m for c in chunk for m in c.mutex})} if any(c.mutex for c in chunk) else {}),
                    "key": _key_record(caseenv.resolve(chunk[0])),
                }
            )
    for case in (c for c in cases if c.test.type != SQLLOGIC):
        batches.append(
            {
                "id": f"b{len(batches) + 1:03d}",
                "runner": PYTHON_RUNNER,
                "order": len(batches) + 1,
                "cases": [case.id],
                "tests": [case.test.root_path],
                "timeout": case.test.timeout or sqllogic.DEFAULT_TIMEOUT_S,
                "retries": {case.id: case.test.retries} if case.test.retries else {},
                **({"mutex": sorted(case.mutex)} if case.mutex else {}),
                "key": _key_record(caseenv.resolve(case)),
            }
        )
    for batch in batches:
        for name in ("timeouts", "retries"):
            if not batch.get(name):
                batch.pop(name, None)
    return {
        "schema": PLAN_SCHEMA,
        "made_at": events.now(),
        "knobs": {
            "workers": workers,
            "batch_size": batch_size,
            "runner": runner,
            **({"run_args": run_args} if run_args else {}),
            "keep": keep,
        },
        "batches": batches,
    }


def write_plan(run_dir, doc, *, force=False):
    return write(run_dir, PLAN_FILE, doc, force=force)


def read_plan(run_dir):
    return read(run_dir, PLAN_FILE, PLAN_SCHEMA, made_by="plan")


def ids_of_plan(doc):
    return [case_id for batch in doc.get("batches") or [] for case_id in batch["cases"]]


def narrow(doc, *, batch_ids=(), case_ids=None):
    """The plan with only the batches named, and inside them only the cases named. A batch that
    keeps no case is left out, so a slice of a plan is a plan."""
    batches = []
    for batch in doc.get("batches") or []:
        if batch_ids and batch["id"] not in batch_ids:
            continue
        keep = [c for c in batch["cases"] if case_ids is None or c in case_ids]
        if not keep:
            continue
        kept = dict(batch, cases=keep)
        if len(keep) != len(batch["cases"]):
            by_id = dict(zip(batch["cases"], batch.get("tests") or batch["cases"]))
            kept["tests"] = [by_id[c] for c in keep]
        batches.append(kept)
    if unknown := sorted(set(batch_ids) - {b["id"] for b in doc.get("batches") or []}):
        raise UsageError(f"the plan has no batch {', '.join(unknown)}; it has {len(doc.get('batches') or [])}")
    return dict(doc, batches=batches)


# -- 03: the services ---------------------------------------------------------


def _record_of(p, *, owned):
    """What `03` says about one service: how it was reached, whether this run owns the instance, its
    block with every secret masked, and the argument that finds it again."""
    return {
        "mode": p.mode,
        "detached": bool(p.service.detached),
        "owned": owned,
        "block": p.redacted(),
        "attach": attach_overrides(p),
    }


def write_provision(run_dir, provisioned, errors, *, keep_services=False, force=False):
    doc = {
        "schema": PROVISION_SCHEMA,
        "made_at": events.now(),
        "services": {
            sid: _record_of(p, owned=bool(p.started and not keep_services)) for sid, p in sorted(provisioned.items())
        },
    }
    if errors:
        doc["errors"] = dict(sorted(errors.items()))
    return write(run_dir, PROVISION_FILE, doc, force=force)


def read_provision(run_dir):
    path = os.path.join(run_dir, PROVISION_FILE)
    if not os.path.isfile(path):
        return {}
    return read(run_dir, PROVISION_FILE, PROVISION_SCHEMA, made_by="provision")


def provision_services(service_ids, project, *, existing_services=(), keep_services=False, separate=False, on=None):
    """Bring up each service, in id order; `on(id, **fields)` is told what happened to each one.
    Returns (provisioned, {service id: why it is not there}).

    `separate` is the `provision` subcommand, which is not the process that runs the tests: it
    refuses a service that cannot outlive it, and never stops what it started."""
    provisioned, errors = {}, {}
    for service_id in sorted(set(service_ids)):
        service = project.services[service_id]
        existing = existing_overrides(service_id, existing_services)
        if separate and existing is None and service.start is not None and not service.detached:
            raise UsageError(
                f"service {service_id!r} lives in the process that starts it, so a separate `provision` would stop "
                f"it again at once. Run `ducktest5 exec --run-dir DIR --provision`, which provisions and runs in "
                f"one process, or name a running instance with --existing-service {service_id}."
            )
        if on:
            on(service_id, state="starting")
        try:
            provisioned[service_id] = provision(service, existing=existing, keep=keep_services or separate)
            if on:
                on(
                    service_id,
                    state="up",
                    mode=provisioned[service_id].mode,
                    block=provisioned[service_id].redacted(),
                )
        except Exception as exc:
            errors[service_id] = f"{type(exc).__name__}: {exc}"
            if on:
                on(service_id, state="failed", error=errors[service_id])
    return provisioned, errors


def attach_from_record(record):
    """The overrides that find a service `03` recorded, as `--existing-service` would give them. A
    field that was masked is refused: a secret is never written down, so it comes from the
    environment or the command line."""
    overrides = dict(record.get("attach") or {})
    if masked := sorted(k for k, v in overrides.items() if v == MASK):
        raise UsageError(
            f"{PROVISION_FILE} holds {masked} masked, as it must. Name the instance again with "
            f"--existing-service, or put the value in the environment."
        )
    return overrides


def services_for_exec(service_ids, project, *, doc, existing_services=(), on=None):
    """The services of a plan, for an exec that is not the process that provisioned them: every one
    is attached to, never started. A service the command line names wins over what `03` recorded."""
    provisioned, errors = {}, {}
    recorded = doc.get("services") or {}
    for service_id in sorted(set(service_ids)):
        if on:
            on(service_id, state="starting")
        try:
            if service_id in recorded:
                existing = existing_overrides(service_id, existing_services)
                if existing is None:
                    existing = attach_from_record(recorded[service_id])
            elif (existing := existing_overrides(service_id, existing_services)) is None:
                raise RuntimeError(
                    f"no instance of {service_id!r} was provisioned for this run. Run "
                    f"`ducktest5 provision --run-dir DIR` first, or `exec --provision` to provision here."
                )
            p = provision(project.services[service_id], existing=existing)
            p.started = False
            provisioned[service_id] = p
            if on:
                on(service_id, state="up", mode=p.mode, block=p.redacted())
        except Exception as exc:
            errors[service_id] = f"{type(exc).__name__}: {exc}"
            if on:
                on(service_id, state="failed", error=errors[service_id])
    return provisioned, errors


# -- cleanup ------------------------------------------------------------------


def outcomes_of(event_lines):
    """{case id: its last outcome}. A case that started and never ended has none: it was interrupted."""
    out = {}
    for event in event_lines:
        if event.get("event") == events.CASE_END:
            out[event["id"]] = event.get("outcome")
    return out


def unfinished(event_lines):
    """The cases that started and never ended, in the order they started."""
    started, ended = [], set()
    for event in event_lines:
        if event.get("event") == events.CASE_START:
            started.append(event["id"])
        elif event.get("event") == events.CASE_END:
            ended.add(event["id"])
    return [case_id for case_id in started if case_id not in ended]


def _paths_by_outcome(event_lines, run_dir):
    """(what may go, what stays): the directories of the cases, by how each one ended. A batch goes
    only when every case in it passed on its first attempt; anything else is evidence."""
    passed, kept, batches = [], [], {}
    for event in event_lines:
        if event.get("event") != events.CASE_END:
            continue
        good = event.get("outcome") == PASSED
        if batch := event.get("batch"):
            # What a failed attempt printed stays, also when a later attempt passed.
            batches[batch] = batches.get(batch, True) and good and (event.get("attempts") or 1) == 1
        for case_dir in event.get("dirs") or ():
            (passed if good else kept).append(os.path.join(run_dir, case_dir))
    for batch, good in batches.items():
        (passed if good else kept).append(os.path.join(run_dir, batch))
    return passed, kept


def cleanup(run_dir, *, project=None, keep=False, event_lines=None, existing_services=()):
    """What a run leaves behind, by what `04` says about each case: the files of a case that passed
    are removed, the files of a case that failed stay, and a service this run started is stopped.

    Returns {"removed": [...], "kept": [...], "stopped": [...], "problems": [...]}. It may run
    twice: what is not there any more is not a problem, and a service that does not answer is not
    started to be stopped."""
    event_lines = events.read(run_dir) if event_lines is None else event_lines
    passed, stays = _paths_by_outcome(event_lines, run_dir)
    summary = {"removed": [], "kept": [], "stopped": [], "problems": []}
    for path in [] if keep else passed:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            summary["removed"].append(path)
    for path in stays + (passed if keep else []):
        if os.path.exists(path):
            summary["kept"].append(path)
            if not keep:
                # The init SQL of a batch that stays may hold a secret; the file says how to get it back.
                sqllogic.remove_init_sql(path)
    summary["kept"] += [
        uri for event in event_lines if event.get("event") == events.CASE_END for uri in (event.get("kept") or [])
    ]
    summary.update(stop_services(run_dir, project, existing_services=existing_services))
    return summary


def stop_services(run_dir, project, *, existing_services=()):
    """Stop what `03` says this run started. A service it did not start is left alone."""
    doc = read_provision(run_dir)
    stopped, problems = [], []
    for service_id, record in (doc.get("services") or {}).items():
        if not record.get("owned") or project is None or service_id not in project.services:
            continue
        service = project.services[service_id]
        if service.stop is None:
            continue
        try:
            block = service.block(
                **{**attach_from_record(record), **(existing_overrides(service_id, existing_services) or {})}
            )
            if service.alive(block):
                service.stop(block)
                stopped.append(service_id)
        except Exception as exc:
            problems.append(f"service {service_id!r} could not be stopped: {type(exc).__name__}: {exc}")
    return {"stopped": stopped, "problems": problems}


def release_all(provisioned, *, on_error=None):
    """Stop what this process started, at the end of an exec that provisioned."""
    for p in provisioned.values():
        try:
            release(p)
        except Exception as exc:
            if on_error:
                on_error(p, exc)
