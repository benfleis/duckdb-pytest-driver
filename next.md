# next.md — requiring data in S3 declaratively (parked)

Question: can you declare "this test needs this data in S3" the same way `TableSpec` declares a
table, using `rclone`?

Short answer: the **shape** exists, but unlike `TableSpec` there's no ready-made implementation.
You'd write ~60–100 lines.

## Why `TableSpec` works out of the box

It ships with `DuckDBInstantiator`, so the driver already knows how to make one real. There's no
equivalent shipped for object storage.

## What does exist for the S3 case

- `@requires(source=<any object>, name=...)` explicitly accepts a "backend-defined lazy ref" — any
  object the framework doesn't understand, interpreted by your provisioner. That path was built for
  exactly this.
- `rclone.seed()` / `object_prefix()` already implement the same `ro`/`rw` + token model tables use:
  `ro` gives `bucket/name` seeded once and shared, `rw` gives `bucket/<token>/name` per test, using
  the same provision token that namespaces schemas.

## What you'd write

A small spec class and a provisioner filling four hooks.

```python
class S3Data:                      # your lazy ref
    def __init__(self, src): self.src = src

class S3Provisioner(Provisioner):
    def ro_target(self, spec, state):
        return object_prefix(BUCKET, spec.resolved_name(), access="ro")
    def rw_target(self, spec, token, state, dry_run):
        return object_prefix(BUCKET, spec.resolved_name(), access="rw", token=token)
    def instantiate(self, spec, target, dry_run, state):
        seed(REMOTE, spec.source.src, BUCKET, spec.resolved_name(), access=..., token=...)
    def env_for(self, state):
        return {"S3_DATA_DIR": f"s3://{...}"}
```

Then in a driver:

```python
@requires(source=S3Data("duckdb/data/lineitem.parquet"), access="ro", name="lineitem")
def test_x(request, resources):
    run_paired(request, env=resources.env)
```

## The rough edges

The `Provisioner` base is SQL-shaped. `execute(sql)`, `drop_sql`, and `ensure_isolated` all assume
schemas. You can leave `execute` unimplemented as long as your `rw_target` never calls
`ensure_isolated`, and you override `teardown()` to `rclone.purge` instead of dropping a schema.
`make_init_sql` is required but only used by `--repl`, so it emits your `CREATE SECRET`. Workable,
slightly against the grain.

## Whether it's worth it for httpfs

Probably not, for the main corpus. The 45 S3 tests already read fixed paths like
`s3://test-bucket/...`, and moving them to per-test seeded data means editing those bodies, which is
the thing we're trying not to do. Seeding at suite level in `populate` matches what the compose
script does today and requires no `.test` changes.

Keep the `@requires` route for the handful of tests that genuinely need their own isolated data,
where converting that one body to a driver `.py` is a fair trade.

---

# Matrix-cell services

Services are suite-wide. A mixed backend matrix therefore provisions every backend even when only
one cell is selected. Let cells reference suite-declared services, and derive the plan's needed
services from the selected cells. Keep full-suite runs unchanged.
