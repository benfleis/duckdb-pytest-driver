# Matrix testing

Ducktest has two matrix mechanisms:

- `register_suite(matrix=...)` varies the SQLLogic invocation for a suite.
- `@requires_matrix(...)` varies a Python driver's provisioned resource.

They both produce parametrized pytest items, but their `properties` have different meanings.

## Suite matrix

A suite matrix cell has a required `id` and optional invocation `properties`:

```python
{"id": "curl", "properties": {"test_config": "test/configs/httpfs_curl.json"}}
```

The cell ID appears in the pytest item name.

Use `register_suite(matrix=...)` when every SQLLogic body in a suite uses the same configurations:

```python
from ducktest import register_suite


def pytest_configure(config):
    register_suite(
        config,
        "httpfs",
        path="test/sql",
        matrix=[
            {"id": name, "properties": {"test_config": f"test/configs/httpfs_{name}.json"}}
            for name in ("curl", "httplib", "dynamic", "connection_caching")
        ],
    )
```

A collected `.test` body under `test/sql` produces four pytest items. Slow and coverage bodies do
the same when enabled with `--slow`.

Python tests opt into the suite matrix by requesting `matrix_cell` or `resources`:

```python
def test_client(matrix_cell):
    assert matrix_cell["id"] in {"curl", "httplib", "dynamic", "connection_caching"}
```

A Python test that requests neither fixture runs once. This avoids multiplying unrelated setup or
assertion tests merely because they share the suite directory.

## Per-test matrix

Use `@requires_matrix` when one Python driver needs its own provisioning matrix:

```python
from ducktest import TableSpec, requires_matrix, run_paired


@requires_matrix(
    source=TableSpec("id_name").Seed(None),
    access="rw",
    properties={"commit": ["cmt", "plain"]},
)
def test_rw(request, resources):
    run_paired(request, env=resources.env)
```

This creates one pytest item per generated requirement cell. Each key in `properties` is an axis;
the example produces `{"commit": "cmt"}` and `{"commit": "plain"}`. The resulting dictionary is
stored in the cell's `Requirement.properties` for the provisioner to interpret. These are not suite
invocation properties.

`resources` provisions the cell and pulls in `matrix_cell` transitively.

If a test has `@requires_matrix` and belongs to a suite with `matrix=`, the test's own matrix wins.
The two matrices are not multiplied.

## Suite cell properties

The driver handles these suite-cell property names when it invokes the SQLLogic binary. Bare `.test`
items and Python drivers using `run_paired` use the same routing.

| Property | Delivery |
| --- | --- |
| `test_config` | `--test-config <path>`; relative paths use the DuckDB working directory |
| `init_sql` | Included in the generated `--init-sqllogic` file |
| `temp_dir_root` | Prefix for `--temp-dir-root` |
| `local_temp_dir_root` | Prefix for `--local-temp-dir-root` |
| `data_dir` | `--data-dir` |
| `local_data_dir` | `--local-data-dir` |
| Any other key | Environment variable passed to the subprocess |

Temporary roots are invocation-specific. The driver appends the session and batch IDs to
`temp_dir_root` and `local_temp_dir_root`, so different cells do not share writable paths. Data
directories are passed through unchanged.

### Environment references

Use `env_ref` when a cell property depends on an environment value supplied by a service or
credential:

```python
from ducktest import env_ref


{"id": "s3", "properties": {"REMOTE_DATA_URI": env_ref("S3_DATA_URI")}}
```

References are resolved immediately before the subprocess starts. Precedence is:

1. Ambient and adopted service or credential environment
2. The `env=` argument passed to `run_paired`
3. Matrix-cell properties
4. Reference resolution

Ducktest registers every non-reserved subprocess environment variable for SQLLogic `{NAME}`
substitution. Users do not configure `--env-passthrough`.

Matrix properties therefore have final value precedence. A missing reference or reference cycle is
a pytest usage error that names the cell and property.

### Init SQL and test configs

`init_sql` is for SQL that must run before the body, such as a `SET` statement. For suites with
`auto_init_sql=True`, service and credential SQL is emitted first and the cell SQL follows. Both are
written to one `--init-sqllogic` file, so cell SQL can override an earlier suite setting.

Use `test_config` when the existing DuckDB JSON config is authoritative. It can define `on_init`,
`statically_loaded_extensions`, and `skip_tests`; those cannot all be represented by `init_sql`.
Both properties may be used in one cell.

## Reserved environment names

The DuckDB test runner owns names such as `TEMP_DIR` and `DATA_DIR`. Do not use them as matrix
environment properties or in `require-env`. Use backend-specific names such as `S3_TEMP_URI` and
`AZ_DATA_URI`.

The lowercase `temp_dir_root` and `data_dir` matrix properties are not environment variables. They
map to command-line options and are safe to use.
