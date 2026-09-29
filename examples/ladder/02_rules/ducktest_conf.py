import os
import tempfile

from ducktest5 import default, match
from ducktest5.resources import SessionDef, StorageDef

# Under the system temp directory, so a run writes nothing into the repo.
SCRATCH = os.path.join(tempfile.gettempdir(), "ducktest-ladder")

STORAGES = [
    StorageDef("scratch_a", f"{SCRATCH}/scratch_a"),
    StorageDef("scratch_b", f"{SCRATCH}/scratch_b"),
]

SESSIONS = [SessionDef("duck", requires=["parquet"])]

RULES = [
    default(bind={"storage": "scratch_a"}, sessions=["duck"]),
    match("rule_matched.test", tags=["matched"]),
    match("rule_matched_body.py", tags=["matched"]),
    match("own_inputs.py", tags=["matched"]),
    match("paired_own_inputs.py", tags=["matched"]),
    # It reads over the public network, so a bare run leaves it out; `--suite all` runs it.
    match("excluded.test", suites=["!default"]),
]
