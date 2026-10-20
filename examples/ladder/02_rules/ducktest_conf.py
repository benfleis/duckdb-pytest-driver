import os
import tempfile

from ducktest import DuckDBEngineDef, LocalStorageDef, Project, default, match

# Under the system temp directory; a run removes what it wrote there when it ends, but a failed test's place.
SCRATCH = os.path.join(tempfile.gettempdir(), "ducktest-ladder")

SCRATCH_A = LocalStorageDef("scratch_a", f"{SCRATCH}/scratch_a")
SCRATCH_B = LocalStorageDef("scratch_b", f"{SCRATCH}/scratch_b")
DUCK = DuckDBEngineDef("duck", requires=["parquet"])

project = Project(
    engines=[DUCK],
    # A value is a dict of slots: each cell binds the storage and writes under it.
    matrix={
        "storage": {
            "a": {"storage": SCRATCH_A, "temp": SCRATCH_A},
            "b": {"storage": SCRATCH_B, "temp": SCRATCH_B},
        }
    },
    rules=[
        # `duck` is the only DuckDB engine, so it is the default already; `engines=` shows how to name one.
        # `temp=` puts {TEMP_DIR} under the storage; `storage=` alone would not move it.
        default(storage=SCRATCH_A, temp=SCRATCH_A, engines=[DUCK]),
        match("rule_matched.test", tags=["matched"]),
        match("rule_matched_body.py", tags=["matched"]),
        match("own_inputs.py", tags=["matched"]),
        match("paired_own_inputs.py", tags=["matched"]),
        match("matrix_body.py", matrix=["storage"], tags=["matched"]),
        # It reads over the public network, so a bare run leaves it out; `--suite all` runs it.
        match("excluded.test", tags=["opt-in"]),
    ],
)
