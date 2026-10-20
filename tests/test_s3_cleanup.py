"""What a run leaves under an s3 storage, and what cleanup removes: against a store in memory, served
by a fake service found again at the address the run recorded."""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from test_run import go, py_test

from ducktest import phases
from ducktest.resources import RW, Service, ServiceDef, StorageDef
from ducktest.resources.made import Made

STORES = {}  # address -> the store that answers there


class MemoryStore:
    """Object keys, as full URIs."""

    def __init__(self, *keys, fail=None):
        self.keys, self.fail = set(keys), fail

    def list(self, uri):
        prefix = uri.rstrip("/") + "/"
        return sorted({k[len(prefix) :].split("/", 1)[0] for k in self.keys if k.startswith(prefix)})

    def delete(self, uri):
        if self.fail:
            raise RuntimeError(self.fail)
        self.keys = {k for k in self.keys if k != uri and not k.startswith(uri + "/")}

    def make_bucket(self, bucket):
        pass


@dataclass(eq=False)
class FakeS3Def(ServiceDef):
    kind = "fake_s3"
    attributes = ("endpoint", "bucket")

    name: str = "s3"
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = "never"


class FakeS3(Service):
    @property
    def address(self):
        return self.definition.address or f"fake://{self.run_token}"

    def start(self):
        STORES.setdefault(self.address, MemoryStore())

    def dealloc(self):
        STORES.pop(self.address, None)

    def read(self):
        return {"endpoint": "http://store:9000", "bucket": "b"} if self.address in STORES else None

    def remote(self):
        return STORES[self.address]


FakeS3Def.instance = FakeS3

SERVICE = FakeS3Def()
LAKE = StorageDef("lake", "s3://{s3.bucket}/lake", endpoint="{s3.endpoint}", inputs=[SERVICE], retain="on_failure")
TOKEN = "2026-09-30T00-00-00Z--brave-otter-1"
RUN = f"s3://b/lake/temp/{TOKEN}"
OTHER_RUN = "s3://b/lake/temp/2026-09-29T00-00-00Z--other-run-2/k/a.csv"


@pytest.fixture
def store():
    STORES.clear()
    made = Made(TOKEN)
    made.create(SERVICE)
    store = STORES[f"fake://{TOKEN}"]
    store.keys |= {
        f"{RUN}/passed/a.csv",
        f"{RUN}/passed/b/c.csv",
        f"{RUN}/passed2/a.csv",
        f"{RUN}/failed/a.csv",
        f"{RUN}/b001/test_x.test/out.csv",
        f"{RUN}/b002/test_y.test/out.csv",
        OTHER_RUN,
    }
    yield SimpleNamespace(made=made, keys=store)
    STORES.clear()


def lake(made):
    return made.make(LAKE)


# -----------------------------------------------------------------------------
# The storage
#


def test_a_test_s_child_is_its_own_prefix_and_deleting_it_leaves_its_neighbours(store):
    child = lake(store.made).acquire(RW, "passed")
    assert child.root == f"{RUN}/passed"
    child.delete()
    assert sorted(store.keys.list(RUN)) == ["b001", "b002", "failed", "passed2"]


def test_list_finds_what_the_run_wrote_and_nothing_of_another_run(store):
    assert lake(store.made).list(TOKEN) == [f"{RUN}/{n}" for n in ("b001", "b002", "failed", "passed", "passed2")]
    assert lake(store.made).list("never-ran") == []


def test_clear_removes_the_run_but_what_a_failed_test_kept(store):
    kept = [f"{RUN}/failed", f"{RUN}/b002/test_y.test"]
    removed = lake(store.made).clear(TOKEN, keep=kept)
    assert removed == [f"{RUN}/{n}" for n in ("b001", "passed", "passed2")]
    assert store.keys.keys == {f"{RUN}/failed/a.csv", f"{RUN}/b002/test_y.test/out.csv", OTHER_RUN}


def test_sweep_removes_the_run_s_prefix_and_no_other(store):
    assert len(lake(store.made).sweep(TOKEN)) == 5
    assert store.keys.keys == {OTHER_RUN}


def test_an_s3_storage_on_no_service_has_nothing_the_run_can_remove():
    bucket = StorageDef("fake_s3", "s3://ladder-fake").make(run_token=TOKEN)
    assert bucket.list(TOKEN) == [] and bucket.clear(TOKEN) == [] and bucket.sweep(TOKEN) == []
    bucket.acquire(RW, "k").delete()


# -----------------------------------------------------------------------------
# Cleanup, which finds the service again at the address the run recorded
#


def _cleanup(tmp_path, made, event_lines):
    run_dir = tmp_path / TOKEN
    phases.write_provision(str(run_dir), made, {})
    project = SimpleNamespace(storages={"lake": LAKE}, services={"s3": SERVICE}, credentials={})
    return phases.cleanup(str(run_dir), project=project, event_lines=event_lines)


ENDED = [
    {"event": "run_start"},
    {"event": "test_end", "name": "p.py::passed", "outcome": "passed"},
    {"event": "test_end", "name": "f.py::failed", "outcome": "failed", "kept": [f"{RUN}/failed"]},
    {"event": "run_end"},
]


def test_cleanup_after_a_run_removes_the_run_but_what_failed(store, tmp_path):
    summary = _cleanup(tmp_path, store.made, ENDED)
    assert f"{RUN}/passed" in summary["removed"] and summary["problems"] == []
    assert store.keys.keys == {f"{RUN}/failed/a.csv", OTHER_RUN}


def test_cleanup_after_a_killed_run_sweeps_the_run(store, tmp_path):
    summary = _cleanup(tmp_path, store.made, [{"event": "run_start"}])
    assert len(summary["swept"]) == 5
    assert store.keys.keys == {OTHER_RUN}


def test_cleanup_starts_no_store_that_is_gone(store, tmp_path):
    run_dir = tmp_path / TOKEN
    phases.write_provision(str(run_dir), store.made, {})
    store.made.end(failed=False)
    project = SimpleNamespace(storages={"lake": LAKE}, services={"s3": SERVICE}, credentials={})
    summary = phases.cleanup(str(run_dir), project=project, event_lines=ENDED)
    assert summary["removed"] == [] and summary["problems"] == [] and STORES == {}


def test_a_store_that_refuses_to_delete_is_a_problem_of_cleanup(store, tmp_path):
    store.keys.fail = "AccessDenied"
    summary = _cleanup(tmp_path, store.made, ENDED)
    assert summary["problems"] == [
        f"storage 'lake': the run's path {RUN} could not be removed: RuntimeError: AccessDenied"
    ]


# -----------------------------------------------------------------------------
# A run whose cleanup cannot remove what it wrote
#


def test_a_green_run_whose_cleanup_leaves_files_says_where_and_is_red(tmp_path, capsys):
    STORES.clear()
    shared = FakeS3Def(address="fake://shared")
    STORES[shared.address] = MemoryStore(fail="AccessDenied")
    lake = StorageDef("lake", "s3://{s3.bucket}/lake", endpoint="{s3.endpoint}", inputs=[shared])

    def writes(ctx):
        STORES[shared.address].keys.add(f"{ctx.values.temp_dir}/a.csv")

    code, run_dir, report, text = go(tmp_path, [py_test(writes, temp=lake)])
    problem = f"storage 'lake': the run's path s3://b/lake/temp/{run_dir.name} could not be removed: "
    problem += "RuntimeError: AccessDenied"
    assert (code, report["exit_code"], report["counts"]["passed"]) == (1, 1, 1)
    assert report["left_behind"] == [problem]
    assert f"cleanup could not remove:\n  {problem}\n" in text
    assert capsys.readouterr().err.splitlines()[-1] == problem
    STORES.clear()
