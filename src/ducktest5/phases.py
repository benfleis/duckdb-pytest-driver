"""The phases, as files in one run directory.

    01-tests.yaml      scan       the tests a selection gives, and what each one needs
    02-plan.yaml       plan       the batches, the runner of each, and the knobs used
    03-provision.yaml  provision  the services, where each one answers, secrets masked
    04-events.jsonl    exec       one line for each test attempt (`events.py`)
    05-report.*        report     what a reader gets (`reports/`)

Each phase is a subcommand over one `--run-dir`: it reads the files of the phases before it and
writes its own. `ducktest5 run` is the same functions in one process, in that order, so there is one
implementation of each phase and not two.

What crosses an invocation boundary:

- `01` and `02` need no infrastructure and hold no secret. Every path in them is relative to the
  project root, so they can be made on one machine and carried to another; the binaries, which are
  of a machine, are named by `exec` and are not in them.
- `03` holds each service's address and its attributes, secrets masked. A secret is never written
  down: `exec` in a new process adopts each service again at its address, and reads its secrets
  from the instance.
- A service that `provision` may leave running is one that says `detached=True`: it is not in the
  process that starts it (a container, a cloud workspace). One that does not is made by the process
  that uses it, which is `exec` and `run`.
- Engines (Spark, the DuckDB shell) always belong to the exec process.

A phase that finds a file of a schema it does not know stops and says so. A phase that finds its own
output already there refuses, unless `--force` (`exec`: `--resume`, `--only-failed`).
"""

from __future__ import annotations

import hashlib
import os
import shutil

import yaml

from . import events, sqllogic, testenv
from .binary import UsageError
from .model import SQLLOGIC
from .outcomes import BLOCKED, ERROR, FAILED, PASSED
from .provision import EXISTING, MANAGED, record
from .resources.made import services_in

TESTS_FILE, PLAN_FILE, PROVISION_FILE = "01-tests.yaml", "02-plan.yaml", "03-provision.yaml"
TESTS_SCHEMA = PLAN_SCHEMA = PROVISION_SCHEMA = 1

PYTHON_RUNNER, UNITTEST_RUNNER, RUN_RUNNER = "python", "unittest", "run"
RUNNERS = (UNITTEST_RUNNER, RUN_RUNNER)  # what `--runner` may say; a Python body is always `python`

KEPT_OUTCOMES = (FAILED, ERROR, BLOCKED)  # what a test must end with for its files to stay


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


# -- 01: the tests ------------------------------------------------------------


def write_tests(run_dir, tests, project, *, working_dir=".", selection=None, force=False):
    """`01-tests.yaml`: every selected test with its test environment as far as it is known with no
    service running. Fields: `schema`, `made_at`, `project` (the root's name), `working_dir` (where
    the paths were read from, relative to the project root), `selection` (what was asked for), and
    `tests`, one record for each test: `identity` (id, test, type, file, params, variants),
    `selection` (tags, and the suites the test declares itself in or out of), `preconditions`
    (services, engines, mutex), `runtime` (params, environment,
    timeout, retries) and `why` (the rules that gave the test what it has)."""
    doc = {
        "schema": TESTS_SCHEMA,
        "made_at": events.now(),
        "project": os.path.basename(os.path.abspath(project.root)),
        "working_dir": os.path.relpath(os.path.abspath(working_dir), project.root).replace(os.sep, "/"),
        "selection": selection or {},
        "tests": [c.record(project) for c in tests],
    }
    return write(run_dir, TESTS_FILE, doc, force=force)


def read_tests(run_dir):
    return read(run_dir, TESTS_FILE, TESTS_SCHEMA, made_by="scan")


def ids_of_tests(doc):
    return [t["identity"]["id"] for t in doc.get("tests") or []]


# -- 02: the batches ----------------------------------------------------------


def _alone(test):
    """A slow file takes a process for itself; a test with a service runs alone, so that a failure
    that runs again for its text does not see what the first attempt left on the service. A test
    that names a mutex takes its batch with it into the lock, so it is one test."""
    return "slow" in test.tags or bool(test.services) or bool(test.mutex)


def _chunks(group, batch_size):
    """The tests of one environment, in batches of at most `batch_size`. A file is in a batch once:
    the binary reports by file name, so two tests of one file could not be told apart."""
    chunks, alone = [], [[c] for c in group if _alone(c)]
    for c in (c for c in group if not _alone(c)):
        chunk = next(
            (
                k
                for k in chunks
                if len(k) < batch_size and c.definition.body_file not in {x.definition.body_file for x in k}
            ),
            None,
        )
        if chunk is None:
            chunks.append(chunk := [])
        chunk.append(c)
    return chunks + alone


def _key_record(test_env):
    """What makes two batches different, for a reader: a short digest of the batch key and the test
    environment that key is made of, secrets masked."""
    key = test_env.key()
    return {"digest": hashlib.sha1(key.encode()).hexdigest()[:12], "environment": test_env.record()}


def build_plan(tests, *, batch_size=10, workers=4, runner=UNITTEST_RUNNER, run_args="", keep=False):
    """`02-plan.yaml` as a document. Tests of `.test` files with one test environment share a batch; a Python
    body is a batch of one, whatever `--runner` says, because only this process can run it.

    A batch: `id`, `runner`, `order`, `tests` (the test ids), `files` (the `.test` files, relative to
    the project root), `timeout` and `timeouts`/`retries` where a test has its own, `mutex` where a
    test names one, and `key` (the digest and the test environment the batch is made of). `knobs`
    records what the planner was given; the batch key is worked out again before a batch runs, so a
    plan can be read by another engine without being believed."""
    if runner not in RUNNERS:
        raise UsageError(f"--runner {runner!r}; it is one of {RUNNERS}")
    batches, groups = [], {}
    for test in (c for c in tests if c.definition.type == SQLLOGIC):
        groups.setdefault(testenv.resolve(test).key(), []).append(test)
    for group in groups.values():
        for chunk in _chunks(group, batch_size):
            batches.append(
                {
                    "id": f"b{len(batches) + 1:03d}",
                    "runner": runner,
                    "order": len(batches) + 1,
                    "tests": [c.id for c in chunk],
                    "files": [c.definition.root_path for c in chunk],
                    "timeout": sqllogic.DEFAULT_TIMEOUT_S,
                    "timeouts": {c.id: c.definition.timeout for c in chunk if c.definition.timeout},
                    "retries": {c.id: c.definition.retries for c in chunk if c.definition.retries},
                    **({"mutex": sorted({m for c in chunk for m in c.mutex})} if any(c.mutex for c in chunk) else {}),
                    "key": _key_record(testenv.resolve(chunk[0])),
                }
            )
    for test in (c for c in tests if c.definition.type != SQLLOGIC):
        batches.append(
            {
                "id": f"b{len(batches) + 1:03d}",
                "runner": PYTHON_RUNNER,
                "order": len(batches) + 1,
                "tests": [test.id],
                "files": [test.definition.root_path],
                "timeout": test.definition.timeout or sqllogic.DEFAULT_TIMEOUT_S,
                "retries": {test.id: test.definition.retries} if test.definition.retries else {},
                **({"mutex": sorted(test.mutex)} if test.mutex else {}),
                "key": _key_record(testenv.resolve(test)),
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
    return [test_id for batch in doc.get("batches") or [] for test_id in batch["tests"]]


def narrow(doc, *, batch_ids=(), test_ids=None):
    """The plan with only the batches named, and inside them only the tests named. A batch that
    keeps no test is left out, so a slice of a plan is a plan."""
    batches = []
    for batch in doc.get("batches") or []:
        if batch_ids and batch["id"] not in batch_ids:
            continue
        keep = [c for c in batch["tests"] if test_ids is None or c in test_ids]
        if not keep:
            continue
        kept = dict(batch, tests=keep)
        if len(keep) != len(batch["tests"]):
            by_id = dict(zip(batch["tests"], batch.get("files") or batch["tests"]))
            kept["files"] = [by_id[c] for c in keep]
        batches.append(kept)
    if unknown := sorted(set(batch_ids) - {b["id"] for b in doc.get("batches") or []}):
        raise UsageError(f"the plan has no batch {', '.join(unknown)}; it has {len(doc.get('batches') or [])}")
    return dict(doc, batches=batches)


# -- 03: the services ---------------------------------------------------------


def write_provision(run_dir, made, errors, *, owned=False, recorded=None, force=False):
    """`03`: each service of `made`, and why each one that is not up is not. `owned` says cleanup
    ends what this process made (the `provision` subcommand); `recorded` are the records of an
    earlier `03`, kept as they are for a service this process adopted from them."""
    services = dict(recorded or {})
    for service_id, service in made.services.items():
        if service_id not in services or made.modes[service_id] != EXISTING:
            mode = made.modes[service_id]
            services[service_id] = record(service, mode, owned=owned and mode == MANAGED)
    doc = {"schema": PROVISION_SCHEMA, "made_at": events.now(), "services": dict(sorted(services.items()))}
    if errors:
        doc["errors"] = dict(sorted(errors.items()))
    return write(run_dir, PROVISION_FILE, doc, force=force)


def read_provision(run_dir):
    path = os.path.join(run_dir, PROVISION_FILE)
    if not os.path.isfile(path):
        return {}
    return read(run_dir, PROVISION_FILE, PROVISION_SCHEMA, made_by="provision")


def services_of(tests):
    """The services behind the inputs of `tests`, inputs first, each once."""
    return services_in(d for test in tests for d, _ in test.inputs)


# -- cleanup ------------------------------------------------------------------


def outcomes_of(event_lines):
    """{test id: its last outcome}. A test that started and never ended has none: it was interrupted."""
    out = {}
    for event in event_lines:
        if event.get("event") == events.TEST_END:
            out[event["id"]] = event.get("outcome")
    return out


def unfinished(event_lines):
    """The tests that started and never ended, in the order they started."""
    started, ended = [], set()
    for event in event_lines:
        if event.get("event") == events.TEST_START:
            started.append(event["id"])
        elif event.get("event") == events.TEST_END:
            ended.add(event["id"])
    return [test_id for test_id in started if test_id not in ended]


def _paths_by_outcome(event_lines, run_dir):
    """(what may go, what stays): the directories of the tests, by how each one ended. A batch goes
    only when every test in it passed on its first attempt; anything else is evidence."""
    passed, kept, batches = [], [], {}
    for event in event_lines:
        if event.get("event") != events.TEST_END:
            continue
        good = event.get("outcome") == PASSED
        if batch := event.get("batch"):
            # What a failed attempt printed stays, also when a later attempt passed.
            batches[batch] = batches.get(batch, True) and good and (event.get("attempts") or 1) == 1
        for test_dir in event.get("dirs") or ():
            (passed if good else kept).append(os.path.join(run_dir, test_dir))
    for batch, good in batches.items():
        (passed if good else kept).append(os.path.join(run_dir, batch))
    return passed, kept


def cleanup(run_dir, *, project=None, keep=False, event_lines=None):
    """What a run leaves behind, by what `04` says about each test: the files of a test that passed
    are removed, the files of a test that failed stay, and a service this run started is stopped.
    A run that never ended (it was killed) did not end the services behind its storages: what
    carries its run token is removed.

    Returns {"removed": [...], "kept": [...], "stopped": [...], "swept": [...], "problems": [...]}. It may run
    twice: what is not there any more is not a problem, and a service that does not answer is not
    started to be stopped."""
    event_lines = events.read(run_dir) if event_lines is None else event_lines
    passed, stays = _paths_by_outcome(event_lines, run_dir)
    summary = {"removed": [], "kept": [], "stopped": [], "swept": [], "problems": []}
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
        uri for event in event_lines if event.get("event") == events.TEST_END for uri in (event.get("kept") or [])
    ]
    failed = any(e.get("outcome") in (FAILED, ERROR) for e in event_lines if e.get("event") == events.TEST_END)
    summary.update(end_services(run_dir, project, failed=failed))
    if project is not None and not keep and not any(e.get("event") == events.RUN_END for e in event_lines):
        summary["swept"] = sweep_services(run_id_of(run_dir), project)
    return summary


def sweep_services(run_token, project):
    """Delete what the project's services made under `run_token`; returns what it found. A service
    that takes inputs is not swept: it cannot be made without them."""
    swept = []
    for d in project.services.values():
        if d.address is not None or d.inputs:
            continue
        service = d.make(run_token=run_token)
        found = service.list(run_token)
        if found:
            service.delete()
            swept += found
    return swept


def end_services(run_dir, project, *, failed):
    """End what `03` says cleanup owns: a service `provision` made, found again by the run's token.
    What its `retain` keeps after a failed run stays. One that is not there any more is left alone."""
    doc = read_provision(run_dir)
    stopped, problems = [], []
    for service_id, entry in (doc.get("services") or {}).items():
        if not entry.get("owned") or project is None or service_id not in project.services:
            continue
        definition = project.services[service_id]
        if definition.inputs:
            continue
        try:
            service = definition.make(run_token=run_id_of(run_dir))
            if service.read() is not None:
                service.end(failed)
                stopped.append(service_id)
        except Exception as exc:
            problems.append(f"service {service_id!r} could not be stopped: {type(exc).__name__}: {exc}")
    return {"stopped": stopped, "problems": problems}
