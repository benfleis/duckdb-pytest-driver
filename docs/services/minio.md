# `minio` — S3-compatible object store

`ducktest.resources.minio` · key `minio` · fixture `minio` · **docker**

The S3 sibling of azurite (the worked example in [../SERVICES.md](../SERVICES.md)). duckdb consumes it as:

```sql
CREATE SECRET (TYPE S3, KEY_ID …, SECRET …, ENDPOINT '127.0.0.1:9000', URL_STYLE 'path', USE_SSL 0);
```

`to_init_sql=minio_init_sql` emits exactly that secret, named `ducktest_minio`.

## Block

| field | default | |
|---|---|---|
| `access_key` / `secret_key` | `admin` / `password` | local dev creds, see below |
| `region` | `us-east-1` | |
| `port` | `9000` | |
| `endpoint` | `http://127.0.0.1:9000` | scheme'd — for rclone + the health probe |
| `s3_endpoint` | `127.0.0.1:9000` | **derived** — host:port, what duckdb's `ENDPOINT` wants |
| `url_style` | `path` | fixed: local MinIO can't do virtual-host addressing |
| `use_ssl` | `False` | fixed: http |
| `bucket` | `ducktest` | |
| `container` | `ducktest-minio` | the `docker exec` target for `mc` |
| `mc_alias` | `ducktest` | alias name `mc` uses inside the container |

Auth varies per consumer, so everything is parameterized: ice-fixture and ducklake use `admin`/`password`
on `:9000`, ice-nessie `minioadmin`/`minioadmin` on `:9002`. Override via `use_service(MINIO_SERVICE, …)`,
`--existing-service minio={json}`, or the `DUCKTEST_MINIO_*` env.

> These are local dev credentials, not secrets — the emulator authenticates against exactly these, and
> they're overridable, so the default is config.

## Env

| AWS-SDK view | explicit `S3_*` view |
|---|---|
| `AWS_ACCESS_KEY_ID` | `S3_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` | `S3_SECRET_ACCESS_KEY` |
| `AWS_REGION` | `S3_REGION` |
| `AWS_ENDPOINT_URL` (scheme'd) | `S3_ENDPOINT` (host:port) |
| | `S3_URL_STYLE`, `S3_USE_SSL` |

The AWS names are what glue/boto/pyarrow read; the `S3_*` names are what a body substitutes into
`SET s3_*` / `CREATE SECRET`. A suite layers its own on top via `to_env`.

## Boot

- **Always-create** (`docker rm -f` first) so a container leaked by an interrupted run can't wedge the port.
- **`/data` is a tmpfs** (`--tmpfs /data:size=2g`, `DUCKTEST_MINIO_TMPFS_SIZE`). MinIO's minimum-free-drive
  check otherwise trips on the host's docker storage and returns `507 XMinioStorageFull` (found live,
  2026-07-16). RAM-backed gives it a clean sized drive regardless of host disk, and it's ephemeral. Raise
  the cap for large seed datasets.
- **Bucket pre-create** via rclone — MinIO auto-creates nothing, and any write to a missing bucket is
  `NoSuchBucket`. Idempotent.
- **Two failure points after `docker run`** (readiness and the bucket create; azurite has only the first).
  Both `docker rm -f` the half-booted container before re-raising, since `start` raises before the block is
  stored and `_stop_services` would never reach it.

`alive` polls `/minio/health/ready` and requires 200 — the endpoint is authoritative, so a 503 during
startup means not ready.

## `mc` helpers

The image carries the `mc` client (see below), so admin operations run via `docker exec` rather than a
second container. All of these are idempotent and set the alias first.

| | |
|---|---|
| `minio_mc(block, *args)` | the single `docker exec … mc` entry point; everything else composes through it |
| `minio_alias_set(block)` | configure `mc_alias` inside the container |
| `minio_user_add(block, user, password)` | create or update a user |
| `minio_policy_attach(block, policy, *, user)` | attach a canned policy |
| `minio_version_enable(block, bucket)` | enable bucket versioning |
| `minio_anonymous_set(block, policy, bucket)` | anonymous access, e.g. `policy='public'` |

An **attached** service (`--existing-service`) must pass a `container` override whose image contains `mc`,
or these raise.

## rclone

MinIO always needs explicit creds + endpoint, and the `endpoint` value (`http://…` — the `:` and `/`)
mangles rclone's inline address form, as would a real secret key. So a MinIO remote must go through a
**config-file stanza** (`write_conf` + `config=`), never inline. That's what the bucket pre-create does and
what real seeding must do.

```python
remote = minio_rclone(block)   # type=s3, provider=Minio, creds + endpoint + region
```

## Image

We ship our own image, not a mirror of upstream's: `resources/images/minio/Dockerfile` layers the `mc`
client into the MinIO server image. A substitute via `DUCKTEST_MINIO_IMAGE` must also carry `mc`.

| | |
|---|---|
| kind | `build` — `publish-images` builds the Dockerfile, it doesn't copy a ref |
| source | `resources/images/minio` |
| pin | `RELEASE.…-mc…` — names both upstreams the Dockerfile bakes in |
| served | follows `DUCKTEST_IMAGE_SOURCE`; per-instance override `DUCKTEST_MINIO_IMAGE` |

Bump the pin whenever either Dockerfile `ARG` moves, or a rebuild overwrites a published tag with
different contents.

```bash
ducktest build-images            # local build, tagged as the served ref
ducktest publish-images --push   # one buildx call: every arch, manifest pushed
```

`build-images` uses the served ref as its tag, so a local build and the published image are
interchangeable — docker runs the local one when it exists and pulls when it doesn't. A stale local build
therefore shadows a newer published one.

## Knobs

`DUCKTEST_MINIO_ACCESS_KEY`, `_SECRET_KEY`, `_REGION`, `_BUCKET`, `_IMAGE`, `_PIN`, `_CONTAINER`
(`ducktest-minio`), `_S3_PORT` (9000), `_CONSOLE_PORT` (9001), `_TMPFS_SIZE` (2g), `_READY_TIMEOUT_S` (20).
Namespace and source are global: `DUCKTEST_IMAGE_NS`, `DUCKTEST_IMAGE_SOURCE`.

## Gap: httpfs's S3 suite needs more than the defaults

The httpfs corpus (45 files) was built against a bespoke compose stack. Unmet by the defaults: region
`eu-west-1`, the legacy `minio_duckdb_user` credentials plus a **second** user, three buckets
(`test-bucket` with **versioning**, `test-bucket-2`, `test-bucket-public` with an anonymous policy), and
virtual-host addressing via `test-bucket.duckdb-minio.com` hostnames.

The `mc` helpers cover the second user, versioning and the anonymous policy; bucket layout and policy
belong in a suite's `populate`, which nothing wires up yet. Virtual-host addressing has no mechanism.
