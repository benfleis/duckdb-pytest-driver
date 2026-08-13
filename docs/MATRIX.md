# Matrix — one `.test` body, N cells

Run the same test body across a set of **cells** — different backends (azurite vs cloud), different
run configs (curl vs httplib client), different storage/commit types. Each cell becomes its own
pytest item, with per-cell environment and, where it applies, a per-cell duckdb `--test-config`.

There are two entry points; pick by scope.

## 1. Suite-wide — `register_suite(matrix=...)`

Every `.test` (and `.py`) in the suite fans across the cells. This is the right tool when a whole
directory of tests should run under each config/backend — e.g. httpfs's curl / httplib / dynamic /
connection-caching configs, or azure's azurite vs cloud.

```python
# test/conftest.py
from ducktest import register_suite

def pytest_configure(config):
    register_suite(
        config, "httpfs", path="test/sql", marker="httpfs", default=True,
        matrix=[
            {"backend": name, "properties": {"test_config": f"test/configs/httpfs_{name}.json"}}
            for name in ("curl", "httplib", "dynamic", "connection_caching")
        ],
    )
```

Every body under `test/sql` now runs as `…[curl]`, `…[httplib]`, `…[dynamic]`,
`…[connection_caching]`. A cell is a dict; `backend` (required, non-empty) is the cell id, and
`properties` (optional) is what each cell needs (see [Cell properties](#cell-properties)).

## 2. Per-test — `@requires_matrix(...)`

One body fans; the rest of the suite doesn't. This is for a body whose *provisioning* varies (UC's
managed vs plain table), not a suite-wide config sweep.

```python
from ducktest import requires_matrix, TableSpec, run_paired

@requires_matrix(source=TableSpec("id_name").Seed(None), access="rw",
                 properties={"commit": ["cmt", "plain"]})
def test_rw(request, resources):
    run_paired(request, env=resources.env)
```

→ `test_rw[cmt]`, `test_rw[plain]`, each with its own `@requires` cell.

### Composition (per-test wins outright)

If a test carries its own `@requires_matrix`, it **replaces** the suite matrix for that test — it is
not multiplied. This is deliberate: the test author declares the *exact* cell set they want, which
can be a **subset** of the suite (this body only runs one backend) or a **superset** (it adds an
extra cell). Auto-multiplying couldn't express the subtractive case.

## Cell properties

A cell's `properties` dict is homogeneous, backend-declared vocabulary. The driver — not the
conftest — decides how each key is delivered:

| property | delivered as | for |
| --- | --- | --- |
| `test_config` | `--test-config <path>` (resolved against the working dir) | a duckdb test-config JSON: `on_init` SQL + `statically_loaded_extensions` + `skip_tests` |
| `init_sql` | merged into the item's `--init-sqllogic` preamble | inline SQL a cell runs before the body (e.g. `SET x='y';`) — the lighter alternative to `test_config` for a pure-`SET` cell |
| `temp_dir_root` | `--temp-dir-root` prefix | the cell's scratch/write root (e.g. a per-backend `az://…`) |
| `data_dir` | `--data-dir` | the cell's read-data root |
| *anything else* | a literal **env var** in the body's environment | `${VAR}` a `.test` body reads (e.g. `AZ_STORAGE_ACCOUNT`, `S3_ENDPOINT`) |

`test_config` is the lever for a run-config sweep (httpfs): the JSON already encodes the `SET`
preamble, the extension-loading mode, and per-config skips — so you reuse your existing
`--test-config` files wholesale, no re-expression in Python.

**`init_sql` vs `test_config`.** `init_sql` covers *only* the preamble SQL. `test_config` also carries
the **loading mode** (`statically_loaded_extensions`) and **per-config skips** (`skip_tests`). So a
cell that changes only `SET`s (e.g. httpfs's curl / httplib client) can use `init_sql`; a cell that
changes extension loading (httpfs's `dynamic`) or skips tests *must* use `test_config` — there's no
`SET` for "load this extension dynamically." They compose: a cell picks whichever fits, and `init_sql`
merges *after* any suite `auto_init_sql` (credentials/service secrets) into one snippet.

### The `TEMP_DIR` / `DATA_DIR` name clash — use prefixed names

duckdb's C++ test harness **permanently registers** `TEMP_DIR` and `DATA_DIR` for its own local
scratch/data paths. A `.test` file's `require-env TEMP_DIR` against an already-registered name
hard-fails at parse time (`"Environment variable 'TEMP_DIR' has already been defined"`). So a
backend's remote-path env vars must use their **own** names — `AZ_DATA_DIR`, `S3_DATA_DIR`, etc. — not
the bare `DATA_DIR`/`TEMP_DIR`. (The `temp_dir_root`/`data_dir` *properties* above are fine — those
become CLI args, not env vars.)

## Out of scope here

Two adjacent design areas are catalog/imperative concerns and don't touch filesystem or config
matrices:

- **Cell-schema granularity** (per-run vs per-test schema names) — only relevant to catalog-backed
  cells (UC/Databricks), where each cell provisions a SQL schema. Filesystem/config matrices have no
  schema. Current behavior is per-test; see PLAN.md.
- **`Context`** (the `initialize`/`run`/`finalize` imperative-hook object) — declarative `.test`
  bodies never see it. Not specified yet; see PLAN.md.
