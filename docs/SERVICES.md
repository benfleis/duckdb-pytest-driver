# Services

A service is shared infrastructure required by a test suite: an object store, HTTP server, proxy, or
catalog. Suites declare services with `register_suite(..., services=[...])`.

Concrete resources:

- [Azurite](services/azurite.md)
- [MinIO](services/minio.md)
- [HTTP server](services/httpserver.md)
- [mitmproxy](services/mitmproxy.md)
- [certificate authority](services/ca.md)

See [ARCHITECTURE.md](ARCHITECTURE.md) for the wider suite, credential, and provisioner model.

## Declaration

```python
from ducktest import register_suite, service


STORAGE = service(
    "storage",
    start=start_storage,
    stop=stop_storage,
    attach=attach_storage,
    alive=storage_alive,
    to_env=storage_env,
    populate=populate_storage,
    to_init_sql=storage_init_sql,
)


def pytest_configure(config):
    register_suite(config, "remote", path="test/remote", services=[STORAGE])
```

Selected suites are known after collection. Their services are provisioned before tests run and
deduplicated by service key.

## Descriptor

| field | contract |
|---|---|
| `key` | stable identity used for deduplication and CLI selection |
| `start(config)` | start a managed instance and return its block |
| `stop(config)` | stop a managed instance at session end |
| `attach(overrides, config)` | build a block for an existing instance |
| `alive(block)` | cheap reachability probe |
| `depends_on` | service keys that must start first |
| `fixture` | optional session-fixture name |
| `to_env(block)` | environment resolved for items belonging to the declaring suite, and registered for SQLLogic substitution there |
| `populate(block, config)` | idempotent structure and data setup, once per declaring suite |
| `to_init_sql(block, redact=False)` | SQL used by `--repl` and `auto_init_sql` suites |

`start=None` declares an external service with no managed lifecycle. It always attaches with default
overrides and is never stopped by ducktest.

## Multi-Service Dependencies

Dependencies start before their dependents and stop in reverse order. Missing dependencies and cycles
are errors.

## Blocks

A block is a JSON-compatible dictionary of runtime facts such as endpoint, credentials, and storage
roots. Managed and attached instances must return the same block shape.

Use one builder for both paths:

```python
def storage_block(**overrides):
    endpoint = overrides.get("endpoint", "http://127.0.0.1:9000").rstrip("/")
    return {
        "endpoint": endpoint,
        "api_endpoint": f"{endpoint}/api",
    }
```

Derived fields must be recomputed from overrides. Do not export a mutable default block.

## Managed And Existing Services

Managed services use `start`, share one stored block across workers, and run `stop` once on the
controller. `start` single-flights globally through the store: exactly one boot for the whole run,
regardless of how many suites bind the key.

`populate` single-flights separately, and is scoped per DECLARING SUITE rather than per key: a
service shared across suites via `use_service(..., populate=...)` runs each binding suite's own
`populate` exactly once across the whole run (coordinated through the store, not just locally),
cumulatively, against the one booted resource. Suites that diverge in `populate` for a shared key
must not conflict with each other (or must be idempotent against each other) — an incompatible setup
belongs in a separate service, not a shared key.

Existing services use `attach`, run `alive`, and run `populate`. They are not stored or single-flighted
at all — every caller re-attaches and re-populates. `populate` must therefore be idempotent regardless
of path (managed or existing).

Declare an existing service with any of these forms:

```text
--existing-service storage
--existing-service storage=http://host.docker.internal:9000
--existing-service 'storage={"endpoint":"http://host:9000","bucket":"tests"}'

DUCKTEST_EXISTING_SERVICE_STORAGE=http://host.docker.internal:9000
DUCKTEST_EXISTING_SERVICES=storage=http://host:9000;catalog=http://host:8181
```

Entry grammar:

- `KEY`: use service defaults.
- `KEY=URL`: pass `{"endpoint": URL}`.
- `KEY={json}`: pass the JSON object as overrides.

Precedence is CLI, then `DUCKTEST_EXISTING_SERVICE_<KEY>`, then `DUCKTEST_EXISTING_SERVICES`.
Keys are normalized across dashes, underscores, and case.

An override for a service outside the current selection's need-set (a matrix cell's `services=`
narrowed it out — `docs/MATRIX.md`) is inert: no attach or `alive` probe is attempted, not an error.

## Suite Policy

Ready-made descriptors contain lifecycle mechanics. Bind suite-specific environment, population, or
init SQL with `use_service`:

```python
from ducktest import use_service


SUITE_STORAGE = use_service(
    STORAGE,
    to_env=suite_storage_env,
    populate=populate_suite_data,
)
```

`use_service` returns a copy. It does not mutate the shared descriptor.

`to_env` should expose stable connection and DATA/TEMP roots. Matrix properties can map those roots to
the unittest binary's `data_dir`, `local_data_dir`, `temp_dir_root`, and `local_temp_dir_root` inputs.
The binary adds session, batch, and test identity to writable TEMP roots.

## Out-Of-Session Lifecycle

Start declared services without collecting tests:

```text
ducktest provision-service storage
ducktest teardown-service storage
```

`provision-service` leaves the service running and prints an `--existing-service` argument. It skips an
instance when its default attach block passes `alive`. It calls `start` directly and does not run
`populate`.

`teardown-service` calls `stop`. Both commands accept a comma-separated key list or no key for all
declared services.

## Object-Store Seeding

`ducktest.tools.rclone` provides backend-neutral object-store operations:

- `Remote`: backend configuration.
- `mkdir`, `sync`, `purge`, `check`, `ls`: bounded rclone commands.
- `object_prefix(container, name, access=, token=)`: shared `ro` or tokenized `rw` paths.
- `seed(...)`: create and populate a prefix.
- `write_conf(...)`: write an rclone configuration for manual use.

`ro` paths are stable and shared. `rw` paths require a token and use
`<container>/<token>/<name>`.

Backend-specific remote construction belongs in the resource module. See
[Azurite](services/azurite.md#rclone) and [MinIO](services/minio.md#rclone).

## `--repl` Init SQL

`to_init_sql` returns SQL needed after service provisioning, usually a DuckDB secret. It serves:

- `--repl` sessions for reachable suites.
- Bare SQLLogic tests in suites with `auto_init_sql=True`.
- Redacted dry-run output when called with `redact=True`.

If an extension is required, start the returned text with `require <extension>`. This uses the
SQLLogic runner's extension-loading path.
