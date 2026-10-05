import os
import tempfile

from ducktest5 import default, match
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.resources import StorageDef

# Under the system temp directory; a run removes what it wrote there when it ends, but a failed test's place.
SCRATCH = os.path.join(tempfile.gettempdir(), "ducktest-ladder")

STORAGES = [
    StorageDef("scratch_a", f"{SCRATCH}/scratch_a"),
    StorageDef("scratch_b", f"{SCRATCH}/scratch_b"),
]

ENGINES = [duckdb_engine("duck", requires=["parquet"])]

# A value given as a string fills the slot the parameter is named after: here, `storage`.
MATRIX = {"storage": {"a": "scratch_a", "b": "scratch_b"}}

RULES = [
    # `duck` is the only DuckDB engine, so it is the default already; `engines=` shows how to name one.
    default(storage="scratch_a", engines=["duck"]),
    match("rule_matched.test", tags=["matched"]),
    match("rule_matched_body.py", tags=["matched"]),
    match("own_inputs.py", tags=["matched"]),
    match("paired_own_inputs.py", tags=["matched"]),
    match("matrix_body.py", matrix=["storage"], tags=["matched"]),
    # It reads over the public network, so a bare run leaves it out; `--suite all` runs it.
    match("excluded.test", tags=["opt-in"]),
]
