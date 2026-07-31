# `minio` — S3-compatible object store

`ducktest.resources.minio` · key `minio` · fixture `minio` · **docker**

S3-compatible object store service. duckdb consumes it as:

```sql
CREATE SECRET (TYPE S3, KEY_ID …, SECRET …, ENDPOINT '127.0.0.1:9000', URL_STYLE 'path', USE_SSL 0);
```

`minio_init_sql` emits this secret as `ducktest_minio`.

## Block

| field | default | |
|---|---|---|
| `access_key` / `secret_key` | `admin` / `password` | local dev creds, see below |
| `region` | `us-east-1` | |
| `domain` | `None` | optional bare FQDN enabling virtual-host bucket routing |
| `port` | `9000` | |
| `endpoint` | `http://127.0.0.1:9000` | for rclone + health probes |
| `s3_endpoint` | `127.0.0.1:9000` | derived host:port for duckdb `ENDPOINT` |
| `url_style` | `path` | conservative default; a configured domain accepts path and vhost requests |
| `use_ssl` | `False` | fixed: http |
| `bucket` | `ducktest` | |
| `data_prefix` / `temp_prefix` | `data` / `temp` | stable and writable object prefixes |
| `container` | `ducktest-minio` | the `docker exec` target for `mc` |
| `mc_alias` | `ducktest` | alias name `mc` uses inside the container |

Auth varies per consumer, so everything is parameterized: ice-fixture and ducklake use `admin`/`password`
on `:9000`, ice-nessie `minioadmin`/`minioadmin` on `:9002`. Use `minio_service(...)` for an explicitly
configured managed instance, `--existing-service minio={json}` for attach-time overrides, or the
`DUCKTEST_MINIO_*` env for the default `MINIO_SERVICE`.

```python
HTTPFS_MINIO = minio_service(
    access_key="minio_duckdb_user",
    secret_key="minio_duckdb_user_password",
    region="eu-west-1",
    bucket="test-bucket",
    domain="127.0.0.1.nip.io",
)
```

> These are local emulator credentials, not secrets.

## Env

| AWS-SDK view | explicit `S3_*` view |
|---|---|
| `AWS_ACCESS_KEY_ID` | `S3_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` | `S3_SECRET_ACCESS_KEY` |
| `AWS_REGION` | `S3_REGION` |
| `AWS_ENDPOINT_URL` (scheme'd) | `S3_ENDPOINT` (host:port) |
| | `S3_URL_STYLE`, `S3_USE_SSL` |
| | `S3_DATA_URI` (stable `s3://<bucket>/data`) |
| | `S3_TEMP_URI_ROOT` (stable `s3://<bucket>/temp`) |

The AWS names are what glue/boto/pyarrow read; the `S3_*` names are what a body substitutes into
`SET s3_*` / `CREATE SECRET`. A suite layers its own on top via `to_env`.
Map `S3_TEMP_URI_ROOT` to the matrix's `temp_dir_root`; the unittest binary appends
session/batch/test identity and exposes the result as `{TEMP_DIR}`.

## Boot

- **Always-create** (`docker rm -f` first) so stale containers do not keep the port.
- **`/data` is a tmpfs** (`--tmpfs /data:size=2g`, `DUCKTEST_MINIO_TMPFS_SIZE`). MinIO's minimum-free-drive
  check otherwise depends on Docker's host storage. Raise the cap for large seed datasets.
- **Bucket pre-create** via rclone — MinIO auto-creates nothing, and any write to a missing bucket is
  `NoSuchBucket`. Idempotent.
- **Optional virtual-host routing** — `DUCKTEST_MINIO_DOMAIN=<bare-fqdn>` passes `MINIO_DOMAIN` to the
  container and uses `http://<domain>:<port>` as the public endpoint. The domain and its bucket
  subdomains must resolve to the host loopback; for network-enabled tests,
  `127.0.0.1.nip.io` supplies that wildcard DNS behavior.
- **Two failure points after `docker run`**: readiness and bucket creation. Both clean up the container
  before re-raising.

`alive` polls `/minio/health/ready` and requires 200 — the endpoint is authoritative, so a 503 during
startup means not ready.

## `mc` helpers

The image carries the `mc` client, so admin operations run via `docker exec`.

| | |
|---|---|
| `minio_mc(block, *args)` | the single `docker exec … mc` entry point; everything else composes through it |
| `minio_alias_set(block)` | configure `mc_alias` inside the container |
| `minio_user_add(block, user, password)` | create or update a user |
| `minio_policy_attach(block, policy, *, user)` | attach a canned policy |
| `minio_version_enable(block, bucket)` | enable bucket versioning |
| `minio_anonymous_set(block, policy, bucket)` | anonymous access, e.g. `policy='public'` |
| `minio_presigned_url(block, object_path, *, bucket=None, expire="24h")` | create a presigned object download URL |

Attached services must pass a `container` override whose image contains `mc`.

## rclone

MinIO uses a config-file stanza (`write_conf` + `config=`), not rclone's inline remote syntax. The URL and
credentials do not round-trip safely through the inline form.

```python
remote = minio_rclone(block)   # type=s3, provider=Minio, creds + endpoint + region
```

## Image

The MinIO image is built from `resources/images/minio/Dockerfile`, which layers `mc` into the server
image. A `DUCKTEST_MINIO_IMAGE` override must also contain `mc`.

| | |
|---|---|
| kind | `build` |
| source | `resources/images/minio` |
| pin | `RELEASE.…-mc…` — names both upstreams the Dockerfile bakes in |
| served | follows `DUCKTEST_IMAGE_SOURCE`; per-instance override `DUCKTEST_MINIO_IMAGE` |

Bump the pin whenever a Dockerfile `ARG` changes.

```bash
ducktest build-images            # local build, tagged as the served ref
ducktest publish-images --push   # one buildx call: every arch, manifest pushed
```

`build-images` tags the local build as the served ref, so Docker uses the local image when present and
pulls it otherwise.

## Knobs

`DUCKTEST_MINIO_ACCESS_KEY`, `_SECRET_KEY`, `_REGION`, `_BUCKET`, `_DOMAIN`, `_IMAGE`, `_PIN`, `_CONTAINER`
(`ducktest-minio`), `_S3_PORT` (9000), `_CONSOLE_PORT` (9001), `_TMPFS_SIZE` (2g), `_READY_TIMEOUT_S` (20).
Namespace and source are global: `DUCKTEST_IMAGE_NS`, `DUCKTEST_IMAGE_SOURCE`.

## Gap: httpfs's S3 suite needs more than the defaults

The httpfs corpus was built against a bespoke compose stack. Unmet by the defaults: region
`eu-west-1`, the legacy `minio_duckdb_user` credentials plus a **second** user, three buckets
(`test-bucket` with **versioning**, `test-bucket-2`, `test-bucket-public` with an anonymous policy).

The `mc` helpers cover the second user, versioning, and anonymous policy. Bucket layout belongs in a
suite's `populate`. The suite can configure the primary instance and virtual-host coverage directly
with `minio_service(...)`.
