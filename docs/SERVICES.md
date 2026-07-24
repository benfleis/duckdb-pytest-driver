# Services — shared resources, lifecycle, and attach

How a **service** (a docker container / daemon a suite needs — a catalog server, an object-store
emulator, …) is declared, provisioned, shared, and — the new part — **attached to when it's already
running**. This is both the tech spec and the how-to for adding a new service; Azurite (the Azure Blob
Storage emulator) is the worked example throughout.

For where a service sits in the wider model (suites, credentials, the store, `@requires`), read
**[ARCHITECTURE.md](ARCHITECTURE.md)** first — this doc is the service deep-dive.

> **Build status.** P1 (block/derive + **attach** + self-boot) and P2 (`provision-service` /
> `teardown-service` out-of-session commands + the `ducktest` shim) are built. P3's **rclone runner**
> (`ducktest/tools/rclone.py`: verbs, `seed`, ro/rw prefixes, `write_conf`) is built + live-verified; routing
> it through `@requires`/the `Instantiator` seam is the pending step.

## The two lifecycle stances

A service is provisioned in one of two ways, and a test **cannot tell which happened** (that's the
design goal):

- **Managed** — this pytest run owns the lifecycle. The first worker to pull the fixture boots it
  (single-flight via the store), the rest share it, and the controller stops it once at session end.
  This is the default and the original model.
- **Existing (external)** — the service is *already running* (you started it, or it lives on the host
  while pytest runs in a container). The run is **told where it is** and **attaches**: no boot, no
  store coordination, no teardown. This is the `--existing-service` path (below).

The #1 driver for "existing" is **Claude/pytest inside a container, service on the host** — you don't
want the container booting (or killing) a service it doesn't own. "Leave it running across many runs
for speed" is the same mechanism.

## When it's provisioned — disposition, eager env adoption, `populate`

Orthogonal to *who owns* the lifecycle (above) is *when* a service is provisioned — its **disposition**,
`Service.provision`:

| disposition | when | for |
|---|---|---|
| `on_demand` (default) | lazy — first worker to pull its fixture (store single-flight) | `.py`-driven tests |
| `eager` | up front, on **suite-selection**, controller pre-fork | **bare `.test` bodies** (+ `--repl`) |
| `per_test` / `never` | *named, not wired* — **fail loud** if reached (`never` == the attach path) | vocabulary only |

**Why `eager` exists — the bare-`.test` problem.** A driverless `.test` runs straight through the
collector→binary; it pulls **no fixture**, so the lazy path never boots the service or sets its env. So a
service-backed suite of bare `.test` files needs its service **up, populated, and its connection env in
the subprocess environment** before any test runs. `eager` does exactly that — the service analog of
`credential(adopt="env")`:

- **`to_env(block) -> dict`** — a derived env map merged into `os.environ` by *every process that
  provisions the service* (in `provision_service`): the controller on the eager path (pre-fork, so a bare
  `.test` subprocess inherits it), or a worker on the on_demand fixture-pull (its `run_paired` subprocess
  inherits it, since `_invoke` merges `os.environ`). This is how `${AZURE_STORAGE_CONNECTION_STRING}` /
  `${AZ_DATA_DIR}` reach a test. Works for any disposition — but a **bare `.test` needs `eager`**: with no
  `.py` driver there's no fixture to trigger worker-side provisioning for it.
- **`populate(block, config)`** — bring the service to its known initial state (**structure + data** — the
  store-scope analog of the fixture lane's `instantiate`), run **once**. **Disposition-independent** — it
  mutates the *shared service*, not per-process state, so it runs for `on_demand` too (folded into the
  store single-flight boot + the attach path), not just `eager`. **Must be idempotent** (it re-runs
  against an attached, possibly-already-seeded instance).

Eager services boot **through `provision_service`** (single-flighted, attach-aware, `depends_on`-ready),
in `_provision_eager_services` right after the eager-credential fetch — same reachability gate, so an
unrelated selection (a databricks-only run) does **not** eagerly boot azurite.

**`attach` × `eager` compose:** an `--existing-service`-attached eager service **adopts `to_env` and runs
`populate` (idempotent)** but does **not** boot or tear down — env-yes / boot-no / seed-idempotent.

**Binding policy to a shared descriptor — `use_service`.** `provision`/`to_env`/`populate` are *suite
policy*, but `AZURITE_SERVICE` is a **shared** descriptor (reused by azure/delta/uc). So bind, don't bake:

```python
from ducktest import use_service, register_suite
from ducktest.resources.azurite import AZURITE_SERVICE, azurite_env, rclone_remote
from ducktest.tools import rclone

def _populate(block, config):                 # idempotent: containers + data
    r = rclone_remote(block)
    for c in ("testing-private", "testing-public", "writes"): rclone.mkdir(r, c)
    for c in ("testing-private", "testing-public"): rclone.sync("data", r, c)

def _env(block):                              # what a bare .test require-env needs
    return {**azurite_env(block), "AZ_DATA_DIR": "testing-private"}

register_suite(config, "azurite", path="test/azure", marker="azure", default=True,
    services=[use_service(AZURITE_SERVICE, provision="eager", to_env=_env, populate=_populate)])
```

`use_service` returns a *copy* of the shared descriptor with the policy set — the shared one stays generic.

## The block, and the derive contract

A service's runtime facts — endpoint, port, account, connection string — are a **block**: a plain
JSON-able dict the fixture reads. The rule that makes boot and attach interchangeable:

> A service module exposes its block as **one builder function**, `block(**overrides) -> dict`, never
> an exported dict. Boot calls it, attach calls it, future non-default configs call it.

Why a function and not a dict: some fields are **derived** (Azurite's `connection_string` embeds the
endpoint + account + key). Override the endpoint and the connection string *must* be recomputed. A
builder makes an inconsistent block impossible — you can't hand it a stale derived field, because it
always recomputes from the inputs. A shared dict invites three call sites to each forget.

```python
# the azurite module — the WHOLE "service block" knowledge
ACCOUNT = "devstoreaccount1"                       # Azure's PUBLIC well-known emulator account
KEY     = "Eby8vdM02x…KBHBeksoGMGw=="              # …and its fixed PUBLIC key — NOT a secret

def azurite_block(**overrides):
    port     = int(overrides.get("port", 10000))
    account  = overrides.get("account", ACCOUNT)
    key      = overrides.get("key", KEY)
    endpoint = overrides.get("endpoint") or f"http://127.0.0.1:{port}"
    b = {"account": account, "key": key, "port": port, "endpoint": endpoint,
         "blob_endpoint": f"{endpoint.rstrip('/')}/{account}",
         "container": overrides.get("container", "ducktest")}
    b["connection_string"] = _conn_str(b)          # DERIVED — recomputed every call
    return b
```

The **payoff**: self-booted Azurite returns `azurite_block(endpoint=<what it bound>)`; attached Azurite
returns `azurite_block(endpoint=<your url>)`. Identical block shape, correct derived fields, either
way. The fixture and the test are oblivious to which path ran — which is exactly what lets you run
in-container against a host service transparently.

For the common emulators the block is **entirely defaults** (Azurite's account/key are public + fixed;
minio's `minioadmin`/`minioadmin` + endpoint are the same shape). So the external declaration expresses
*nothing but maybe an endpoint*; everything else comes from the module. Non-default account/key later is
the same builder with a non-empty override map — no new code path.

## Attaching to an existing service

Tell a run a service is already up; it attaches instead of booting. Three input channels, one grammar,
per-key merge with precedence **CLI > per-service env > list env**:

| Channel | Form |
|---|---|
| Stacked CLI | `--existing-service azurite=URL --existing-service minio` (repeatable) |
| List CLI | `--existing-service azurite=URL,minio` (`,` or `;` split) |
| Per-service env | `DUCKTEST_EXISTING_SERVICE_AZURITE=1` \| `=URL` \| `={json}` |
| List env | `DUCKTEST_EXISTING_SERVICES=azurite=URL;minio` |

The per-service env var is the cleanest thing to hand a container (`docker run -e …`).

**Entry grammar** (uniform everywhere):

- `KEY` — attach with **all defaults** (service running at its default endpoint).
- `KEY=URL` — attach, **endpoint overridden** (e.g. `host.docker.internal:10000` from a container).
- `KEY={json}` — attach with a **full override map** (the non-default-account case).

For the per-service env var, a bare truthy value (`1`/`true`/`yes`/`on`/empty) means "all defaults" —
so `DUCKTEST_EXISTING_SERVICE_AZURITE=1` is the valueless form. Keys are normalized
(`oss-uc-server` ↔ `OSS_UC_SERVER` ↔ `oss_uc_server`) so a dashed service key still resolves from an
env-var name.

**Attach probe.** On attach, the run calls the service's `alive(block)` once. A declared-but-dead
service **fails loud** ("you pointed azurite at X, nothing's there") instead of letting a test die with
an opaque connection-refused deep in a query — the paradigm-shift stance (selected-but-unprovisionable
FAILS) applied to attach. `alive` is a cheap, non-authenticating probe (any HTTP response = up).

Under the hood: an attached service is **never entered into the store**, so the controller's
end-of-session teardown (which stops only services present in the store) naturally leaves it alone.

## Adding a new service (the how-to)

A service is a `service(...)` descriptor plus a thin session fixture. Everything below is the *whole*
Azurite integration.

```python
# ducktest/resources/azurite.py  (or a backend's own module)
from ducktest import service
from ducktest.resources.azurite import azurite_block, azurite_alive, _start, _stop  # illustrative

AZURITE_SERVICE = service(
    "azurite",
    start=_start,                                   # managed boot: docker run … -> azurite_block(endpoint=bound)
    stop=_stop,                                     # managed teardown (controller, once)
    attach=lambda overrides, config: azurite_block(**overrides),   # existing: build block from overrides
    alive=azurite_alive,                            # liveness probe (attach + future)
    fixture="azurite",
)
```

```python
# the consumer's test/conftest.py or a resources conftest
import types, pytest
from ducktest import register_suite, provision_service
from ducktest.resources.azurite import AZURITE_SERVICE

def pytest_configure(config):
    register_suite(config, "azure", path="test/azure", marker="azure", default=False,
                   services=[AZURITE_SERVICE])

@pytest.fixture(scope="session")
def azurite(request):
    block = provision_service(request.config, AZURITE_SERVICE)   # boots OR attaches — fixture can't tell
    return types.SimpleNamespace(**block)                        # test reads .connection_string / .blob_endpoint / …
```

`provision_service` does all the routing: existing-service declared → `attach` + `alive`; else the
store single-flight boot. The fixture is identical for both stances — that's the point.

The `service()` descriptor fields:

| field | when called | purpose |
|---|---|---|
| `start(config)` | managed, first-need | boot; return a block (dict) or None |
| `stop(config)` | managed, session end | tear down once (controller) |
| `attach(overrides, config)` | existing, per worker | build the block from the override map |
| `alive(block)` | attach (+ future liveness) | cheap reachability probe → bool |
| `fixture` | — | name of the session fixture that backs it |

Only `start` is required. A service with no `attach` can't be externalized (attaching falls back to
using the raw overrides as the block); give it an `attach` to make `--existing-service` meaningful.

## `provision-service` / `teardown-service` (built)

The *other side* of attach: bring a service up out-of-band, leave it running, and hand you the exact
line to attach with — so a later in-container run attaches to it.

```
host:       ducktest provision-service azurite            # boot + leave up + print the attach line
container:  pytest -m azure --existing-service azurite=http://host.docker.internal:10000/
host:       ducktest teardown-service azurite             # stop when done (no key = all)
```

`ducktest provision-service azurite` prints:

```
✓ azurite: up at http://127.0.0.1:10000
    attach: --existing-service azurite=http://127.0.0.1:10000   (or env DUCKTEST_EXISTING_SERVICE_AZURITE=http://127.0.0.1:10000)
```

Mechanics: provisioning needs conftest registration + the test env, which the isolated `ducktest`
config-tool can't see — so the real worker is a **pytest invocation-mode**
(`pytest --provision-service azurite`), run BEFORE any collection (so it needs no unittest binary — works
in an unbuilt checkout). `ducktest provision-service azurite` is a **thin shim** that shells
`python -m pytest --provision-service azurite` in the same interpreter's env (pytest + plugin + test
deps co-installed — the install model). Both accept a comma-list of keys or none (= all). Provisioning
is **idempotent**: an already-`alive` service is skipped, not clobbered. Started services are launched
directly (not through the store), so the normal sessionfinish teardown **leaves them running**.

> **Known gap (2026-07-14):** `provision-service` calls `start` only — it does **not** run `populate`, so
> it currently leaves an *empty* instance (azurite with no containers/data). A later `pytest` run
> repopulates on attach (masking it), but a hand-run against the provisioned instance hits e.g.
> `ContainerNotFound`. Fix is to route it through the same one-shot `populate` (PLAN.md).

This is only a **thin slice** of lifecycle — `provision`/`teardown` — **not** a persistent registry. The
endpoint is carried from provision to attach by *you* (an env var, a script). If that carrying ever gets
annoying, that's when a **registry** (a liveness-probed on-disk record so runs auto-discover a running
service) earns itself — deferred until then, so we build it when we know we need it, not before.

**rclone-config dump** _(P3 — lands with the rclone seeder)_. When `provision-service --seed` seeds an
object store, it will write a real `rclone.conf` to a known temp path
(`/tmp/ducktest-rclone-<key>-XXXX.conf`), name the remote after the service key, and print the path — so
you can `rclone --config … ls azurite:container` by hand. The automated seeder stays file-free (the
env-configured connection string, below); the dump is the human affordance, the same connection string
written as a named remote. It lives with P3 because the stanza is rclone/backend knowledge.

## Object-store seeding — DCL/DML, ro/rw (P3 — runner built; `@requires` wiring pending)

Object stores get the same two-phase, access-moded shape as the SQL fixture lane:

| | SQL lane | object-store lane |
|---|---|---|
| **DCL/DDL** (structure) | `CREATE SCHEMA/TABLE` via the duckdb middleman | create bucket/container + access policy |
| **DML** (data) | `INSERT` / seed rows | `rclone sync <dataset> → remote:container/prefix` |
| **access** | `@requires(access="ro"\|"rw")` | *same decorator, same meaning* |

- **`rw`** → per-test isolated prefix, keyed on the **same per-test provision token** that namespaces
  cell-schemas today, so `container/<token>/…` is collision-free under xdist by construction. Local
  dataset staging happens under the unittest **TEMP_DIR** before rclone pushes it up — it fits that seam
  by design. Created (DCL) + seeded (DML) + torn down.
- **`ro`** → shared dataset, referenced directly. Seeded **once, out-of-band** (a periodic rclone job —
  as you already do by hand), and per-run merely *referenced*, or at most verified with an idempotent
  `rclone check` (cheap — it only diffs). This is the answer to "don't re-upload huge cloud datasets
  every run": RO data is just data that happens to be `.parquet` instead of `INSERT`s, owned outside the
  test run.

**The runner (`ducktest/tools/rclone.py`, built + live-verified).** A generic rclone wrapper: a `Remote`
(name + backend params → an **inline** address `:type,k=v:path` or a **config-file** stanza), the verbs
`mkdir` / `sync` / `purge` / `check` / `ls`, a `seed(remote, src, container, name, access=, token=)`
that does DCL+DML and returns the access-scoped prefix, `object_prefix` (the ro/rw split above), and
`write_conf` (the human `rclone.conf` dump). A *service* supplies only its `Remote` params — azurite's
is `{"type": "azureblob", "use_emulator": True}` (`ducktest.resources.azurite.AZURITE_RCLONE` /
`rclone_remote(block)`).

Two proven facts, both load-bearing:
- **`use_emulator=true`** is the canonical azurite path — rclone fills in the well-known
  account/key/endpoint itself. It's special-char-free, so the **inline** form
  (`:azureblob,use_emulator=true:cont/pfx`) works (unlike a real account key, whose `/`/`==` mangle
  inline parsing — those go through a config file or `rclone_remote(block)`). A **moved/attached**
  instance (non-default endpoint) can't use the emulator shortcut → explicit account/key/endpoint remote.
- **`--skipApiVersionCheck`** is **required** on the Azurite container (the managed boot adds it): a
  pinned/older Azurite build otherwise rejects a newer rclone/SDK client with `API version … not
  supported`. Harmless on a dev emulator; makes seeding immune to client/image skew.

**Wiring (pending).** The runner is standalone today — a conftest/provisioner calls `rclone.seed(...)`
directly. Routing it through **`@requires` → the `Instantiator` seam** (an object-store instantiator that
yields a **URI** into `resources.env` instead of a canonicalized `Table` — no duckdb middleman, per
ARCHITECTURE § *Fixtures* "archive/directory fixtures") is the next step; it should land on top of the
base `Provisioner` refactor. The `rclone.conf` dump from `provision-service --seed` (P2 §) also lands
with that wiring.

## `--repl` init SQL — `to_init_sql`

A `@requires`-driven suite's `--repl` gets a working session for free: the `Provisioner`'s
`make_init_sql(bindings)` builds the `CREATE SECRET`/`USE` DDL fed into duckdb's `-init` file
(ARCHITECTURE.md § *Provisioning per-test tables*). A bare-`.test` suite (azurite, minio, and any
future service/credential-only suite — no `@requires`, no provisioner) had no such thing: `--repl`
dropped you into a shell with the connection env set (via `to_env`/`adopt="env"`) but nothing typed
for you, because DuckDB's own `CREATE SECRET` with no `PROVIDER` never auto-reads env vars.

`Service`/`Credential` close that gap with the same shape as `to_env`:

- **`Service.to_init_sql(block, *, redact=False) -> str`** — e.g. azurite's default
  `CREATE OR REPLACE SECRET ducktest_azurite (TYPE AZURE, CONNECTION_STRING '…')`.
- **`Credential.to_init_sql(value, *, redact=False) -> str`** — same shape, built from the fetched
  credential value instead of a service block.

`--repl` gathers `to_init_sql` from every reachable suite's active services/credentials (dedup by key,
same as `provision_reachable`) and prepends it to whatever a provisioner also contributes — nothing new
is fetched/booted, it only reads back what up-front provisioning already put in the store.
`redact=True` is `--provision-dry-run`'s printed preview (mask the secret material, keep the shape,
mirroring `make_init_sql(redact=True)`). None (the default) => that service/credential contributes no
SQL, same as today.

**Caveat for `credential(available=…, to_init_sql=…)`:** the gather reads back an already-fetched value
from the store, it never re-fetches. If `available()` short-circuits the up-front fetch (creds already
usable via preset env), nothing was ever written to the store, so `--repl` contributes no SQL for that
credential even though it's genuinely usable — no consumer combines the two today, but a future one
should build its `to_init_sql` from env directly rather than assume a store-backed value.

See azurite's/minio's `resources/*.py` (`azurite_init_sql`/`minio_init_sql`) for the
worked examples.

## Layering discipline (why this is mostly generic)

Adding Azurite forced almost nothing azurite-specific. The generic mechanisms — attach declaration +
parsing, the block/derive contract, the `alive` probe hook, (P2) the provision/teardown commands +
conf-dump, (P3) the rclone runner + object-store instantiator + ro/rw — all live in **ducktest core**
(`plugin.py`, `suites.py`, `ducktest.resources`). The Azurite *instance* is just values: image ref, port
map, the public account/key, the connection-string derive, the remote stanza, the datasets. If a new
service ever needs a *mechanism* that isn't there, the mechanism goes in core first — the instance stays
config. minio, an S3-compatible store, a REST catalog: all the same shape.
