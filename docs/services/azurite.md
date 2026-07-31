# `azurite` - Azure Blob Storage emulator

`ducktest.resources.azurite` | key `azurite` | fixture `azurite` | **Docker**

Azurite uses Microsoft's fixed public emulator account and key. They are not deployment credentials
and cannot access Azure.

## Usage

```python
from ducktest import register_suite, use_service
from ducktest.resources.azurite import AZURITE_SERVICE, azurite_env


AZURE_STORAGE = use_service(
    AZURITE_SERVICE,
    to_env=azurite_env,
    populate=populate_test_data,
)


def pytest_configure(config):
    register_suite(
        config,
        "azure",
        path="test/azure",
        services=[AZURE_STORAGE],
        auto_init_sql=True,
    )
```

`populate_test_data(block, config)` must be idempotent because it also runs against attached instances.

## Block

`azurite_block(**overrides)` builds managed and attached blocks.

| field | default |
|---|---|
| `account` | `devstoreaccount1` |
| `key` | Microsoft's fixed emulator key |
| `port` | `10000` |
| `endpoint` | `http://127.0.0.1:10000` |
| `blob_endpoint` | `<endpoint>/<account>` |
| `container` | `ducktest` |
| `connection_string` | derived from account, key, and blob endpoint |

Changing `endpoint`, `account`, or `key` recomputes the connection string.

## Environment

`azurite_env(block)` exports:

| variable | value |
|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | complete Azurite connection string |
| `AZURE_STORAGE_ACCOUNT` | emulator account |
| `AZ_STORAGE_ACCOUNT` | emulator account |

Add suite-specific container names and DATA/TEMP roots in a wrapper `to_env` function.

## Init SQL

`azurite_init_sql(block, redact=False)` creates `ducktest_azurite`, an Azure secret using the block's
connection string. It starts with `require azure` so the SQLLogic runner loads the extension before
creating the secret.

The descriptor supplies this function as `to_init_sql`. Enable `auto_init_sql` for bare `.test` files
that need the secret.

## Lifecycle

Managed startup runs the Azurite image with these host mappings:

| service | host port | container port |
|---|---:|---:|
| Blob | `10000` | `10000` |
| Queue | `10001` | `10001` |
| Table | `10002` | `10002` |

Startup removes a stale container with the configured name, launches a fresh instance, and waits for
the Blob endpoint. A failed readiness check removes the partial container.

The managed command includes `--skipApiVersionCheck`. This avoids failures when a newer SDK or rclone
client uses an API version unknown to the pinned emulator image.

`azurite_alive(block)` treats any HTTP response as alive. An unauthenticated request normally returns
an HTTP error; connection failures return false.

## Existing Instance

Attach to an instance on the default endpoint:

```text
pytest --existing-service azurite
```

Attach from a container to an instance running on the host:

```text
pytest --existing-service azurite=http://host.docker.internal:10000
```

For non-default account or container values, pass JSON:

```text
pytest --existing-service 'azurite={"endpoint":"http://host:10000","container":"tests"}'
```

The attach path rebuilds `blob_endpoint` and `connection_string`, checks liveness, and leaves the
external instance running.

## rclone

`rclone_remote(block=None)` returns the rclone remote used for seeding and cleanup.

The default local instance uses `use_emulator=true`, which is safe in rclone's inline remote syntax.
An attached endpoint uses explicit account, key, and `blob_endpoint` values and therefore requires a
generated rclone config file. The generic rclone wrapper handles that choice.

```python
from ducktest.resources.azurite import rclone_remote
from ducktest.tools.rclone import mkdir, sync


remote = rclone_remote(block)
mkdir(remote, "test-data")
sync("data", remote, "test-data")
```

## Image

The driver mirrors Microsoft's public Azurite image into the configured Ducktest image namespace.
`DUCKTEST_IMAGE_SOURCE` selects the served source. `DUCKTEST_AZURITE_IMAGE` overrides the image for this
resource.

## Knobs

| environment variable | default |
|---|---|
| `DUCKTEST_AZURITE_UPSTREAM` | `mcr.microsoft.com/azure-storage/azurite:latest` |
| `DUCKTEST_AZURITE_PIN` | `2026-07-17` |
| `DUCKTEST_AZURITE_IMAGE` | resolved served image |
| `DUCKTEST_AZURITE_CONTAINER` | `ducktest-azurite` |
| `DUCKTEST_AZURITE_BLOB_PORT` | `10000` |
| `DUCKTEST_AZURITE_QUEUE_PORT` | `10001` |
| `DUCKTEST_AZURITE_TABLE_PORT` | `10002` |
| `DUCKTEST_AZURITE_DEFAULT_CONTAINER` | `ducktest` |
| `DUCKTEST_AZURITE_READY_TIMEOUT_S` | `20` |

Generic service lifecycle and attach behavior are documented in [SERVICES.md](../SERVICES.md).
