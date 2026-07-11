# Design: test tiers + up-front resources (driver-owned)

Status: **design, aligned; not yet implemented.** Home for the model behind the smoke/live
split, credential fetching, and shared services. The mechanism lives in **the driver**
(`duckdb_pytest_driver`, the auto-registered plugin) — *not* the `duck-test` CLI, which is only
ingress/config. Everything here works with plain `pytest`.

**Resolved (2026-07):** resources are carried on one uniform **shared-state store**
(stdlib `multiprocessing.managers`, no dep); credentials are **eager** (pre-fork), services are
**lazy first-need** — which **defers scan-plan** as unnecessary for the near-term path. See
*Shared-state store* and *RESOLVED: store-based lazy provisioning* below.

## ⚠️ PARADIGM SHIFT (read first; shout it to users)

duck-test deliberately breaks with the old duckdb `unittest` / `require-env` norm:

| | old world (`require-env`) | new world (duck-test) |
|---|---|---|
| selection | run everything; hope | **deliberate** — pick a set |
| provisioning | manual; `require-env` gates | **(near) automatic** |
| a *selected* test that can't run | **silent skip** (unremarkable) | **FAILURE** (loud, red, counted) |

The one sentence: **skips that used to happen silently now surface as failures.** A test that is
*selected* but can't be provisioned (missing credential, provision error) FAILS — it does not skip.
Only *deselected* tests (a set not in this run) are absent. This is the biggest surprise for anyone
coming from `require-env`, and it's the whole point: silent skips let "green" mean "didn't run."

## North star (read first)

The overriding goal is **developer experience — simplicity, consistency, predictability** — NOT
reducing pytest config. Config-reduction is a nice side effect that varies by context.

- **Tiering is opt-in per repo.** A repo that declares no tiers gets vanilla `pytest` — everything
  runs, no deselection, no surprises (e.g. duckdb core, which has no special needs). The behavior
  only changes where a project *opts in* by declaring a non-default tier.
- **Any change to bare `pytest` must be loud.** When a tier is deselected, a banner announces it.
- **Control scales in proportion to declaration.** More custom behavior (tiers, up-front creds,
  services) costs proportionally more lines in the repo's `test/conftest.py`, and does no harm when
  scaled together. Direct integrators (UC/Delta/Iceberg) don't mind; vanilla repos pay nothing.

## The problem it solves

Today a backend that needs live resources hand-rolls a subtree `conftest.py` (~100 lines in
`uc/test/databricks/conftest.py`) doing four unrelated jobs: mark items, implement default-selection
(deselect the live tier on a bare run), fetch credentials up front + hard-fail, register a
provisioner. Three of the four are **generic** and should be the driver's.

Worse, the up-front credential fetch is broken for `pytest test` (the **full-suite gap**): under
xdist the controller doesn't collect, so a *subtree* conftest's `pytest_configure` never fires on
the controller unless the invocation **path descends into** that subtree (`pytest test/databricks`).
The **driver plugin's** `pytest_configure`, by contrast, **always** fires on the controller — so
moving tiering into the driver closes the gap for free.

## Model

A **tier** = a named subset of the suite with a default-selection policy and a set of resources.
Resources are delivered + coordinated through one **shared-state store** (next section); each carries
an **eager/lazy policy**, and two shapes recur:

- **class-1 credential** — **eager**: fetched *once, up front, on the controller* (pre-fork), so an
  `op`/biometric prompt lands at invocation, never deep in a run. Eager is a *predictability* policy,
  not a technical limit — `op` pops a GUI dialog, so a worker *could* fetch late; we deliberately don't
  let it. The eager-at-start invariant is **execution-mode-independent**: it holds for a serial
  benchmark run just as much as an xdist run.
- **class-2 service** — **lazy, first-need-wins**: the first worker that needs it provisions + publishes
  to the store; others block on the store (configurable timeout) or fail loud. Torn down once by the
  controller; recovery-at-next-start is the robust net for leaks (the OSS docker container is the model).

## Shared-state store (the resource carrier)

One **uniform carrier** for controller↔worker state, backed by stdlib `multiprocessing.managers` (no
dependency) over a **platform-native socket** (AF_UNIX on posix, AF_PIPE named-pipe on Windows) — no
listening TCP port; **off-disk** (values live in the manager's memory). Started by the controller at
`pytest_configure` (pre-fork); its address + authkey go into `os.environ` so workers connect, torn down
+ reclaimed-stale at session boundaries (mirrors the container lifecycle).

**Portability (Windows CI matters):** use `address=None` (native family, both platforms) and the
**default start context** — spawn on macOS/Windows, fork on Linux; **no `fork` dependency**. The one
requirement spawn imposes: the store module must be **import-clean** (the spawned server re-imports it),
which a normal package module satisfies. Security is a non-goal (local socket; the authkey exists only
because multiprocessing requires one). **Status:** proven by `spikes/store_poc/` and promoted to
`src/duckdb_pytest_driver/store.py` (per-key locks; offline tests green under spawn) — added, pending
review/commit.

**Access verbs, by consumer intent** (require-present and provide-if-missing are genuinely different
intents — overloading one call with a "don't provision" factory read backwards). Values are stored
**JSON-serialized**, so every read hands back a private *copy* — hence the verb:

    put(store, key, block)            owner writes a block (the controller, pre-fork)
    copy(store, key)                  eager consumer -> a private copy, else fail loud
                                      (never provisions, never blocks)
    copy_or_provision(store, key, fn) lazy consumer  -> cached copy, else single-flight provision
                                      (others block; timeout -> ProvisionTimeout)

Creds are **eager**: the controller `put`s them into the store pre-fork, so a worker's `copy()` always
finds them — an absent one is a real failure, not a skip. Services are **lazy**: the first worker to
need one provisions via `copy_or_provision`; the rest block, then read what the winner published.
(Env-adoption — a dev's own env script supplying a credential — is handled at *declaration*: the
controller reads env into the store on the pre-fork pass, so workers still just `copy()`.)

**Value shape — context blocks, not scalars.** A stored value is a whole struct/JSON block (the shape
`credential.fetch` already returns from 1Password): a credential is `{TOKEN, ENDPOINT, REGION, …}`; a
service is `{name, version, state, url, …}`. A couple of type-specific primitives (a `Queue`/`Event`
for coordination) are available, but the default is "publish a whole block." Keep values flat +
picklable and **replace wholesale** — `managers` hands out proxies, and *nested* mutation through a
proxy does **not** propagate (the classic footgun). Don't overbuild this.

**Two comms layers — deliberately not matched.** The store is the *pytest coordinator↔worker* substrate
(rich: KV blocks, queues, events). The *worker→`unittest`* boundary stays dumb: env vars +
test-config/init-sql/`--env-passthrough`. The py worker **decodes** the store blocks it needs into that
flat form right before invoking the binary. Payoff: the `unittest` contract (and `.test` bodies) never
grows as the pytest-side carrier gains features — data flow at the seam is one-directional and flat
(`store → worker decodes → env → unittest`).

**v1 vs v2 (deferred).** v1 is the manager child-process above — trivial, ~1 module, Windows-proven.
A v2 could host the store **in the controller process itself** via a background threaded/async socket
server (no child): it dissolves the child-process fork/spawn question entirely and simplifies the leak
story (the server dies *with* the controller — no orphan to reclaim), at the cost of hand-rolling
framing/auth and running a loop on a side thread. Since v1 is trivial and Windows-safe, **v2 is
deferred** — revisit only if the orphaned-child-on-`SIGKILL` edge actually bites or the carrier needs
richer native protocol (queues/pub-sub). The worker-facing API is just `get_or_provision`, so v1→v2 is
a swap behind that seam, not a rewrite.

## Declaration API

Home: the repo's **`test/conftest.py`** — an *initial* conftest for every `test/…` invocation
(bare, `pytest test`, `pytest test/databricks`), so its `pytest_configure` fires on the controller
early. The repo root stays vanilla; the only root requirement is **`pytest.ini`** (rootdir + config
from `duck-test configure`), not a conftest.

New driver API:
```python
def register_tier(config, name, *, path=None, marker=None, default=True,
                  credentials=(), services=(), provisioner=None) -> None
def credential(key, *, fetch, validate=None, error=None, adopt=None) -> Credential
def service(key, *, start, stop=None, fixture=None) -> Service
```
- **membership** is **path-based**, and the driver **auto-applies a same-named marker** to every
  member — so you declare once by dir and get `-m databricks` / `-m 'not databricks'` for free
  (a strict superset of hand-authored markers; also works for `.test`/SQLLogic bodies that can't
  carry Python markers). (Considered + rejected as primary: marker-authored per-test;
  `@requires`-derived — that ties tiering to provisioning, a different axis.)
- `credential.fetch(config) -> dict|obj` runs once on the controller (matches UC's `load_creds`
  signature). `validate() -> bool` / `error() -> str` feed the hard-fail. `adopt="env"` →
  `os.environ.update(value)` on every process that receives it.
- `service.start()`/`service.stop()` are UC's `start_container`/`teardown_shared`; `fixture="uc_server"`
  links the descriptor to the session fixture (for gating — the body is unchanged).

**Shared resources live in the driver *library*** (`duckdb_pytest_driver.resources`): ready-made
`service()`/`credential()` descriptors for minio/azurite/docker + s3/azure/1Password, imported and
declared by any repo. A generic **env-or-1Password credential** fetch (UC's `load_creds` pattern) is
the first candidate. The `duck-test` CLI hosts none of this — it's optional sugar.

### UC declaration (before → after)

~140 hand-rolled lines across two subtree conftests collapse to ~18 declarative lines:
```python
# uc/test/conftest.py
from driver import register_tier, credential, service
from uc.databricks import DatabricksProvisioner
from uc.databricks.engine import load_creds, have_core_creds, cred_failure_detail
from uc.oss import OssProvisioner
from uc.server import start_container, teardown_shared, uc_server  # noqa: F401 (fixture re-export)

def pytest_configure(config):
    register_tier(config, "oss_local", path="test/oss_local", default=True,
        provisioner=OssProvisioner(config),
        services=[service("oss-uc-server", start=start_container, stop=teardown_shared, fixture="uc_server")])
    register_tier(config, "databricks", path="test/databricks", default=False,
        provisioner=DatabricksProvisioner(config),
        credentials=[credential("databricks_creds", fetch=load_creds,
                     validate=have_core_creds, error=cred_failure_detail, adopt="env")])
```
Everything backend-specific (the creds callables, `start_container`/`teardown_shared`, provisioners,
the `uc_server` fixture body, the autouse env fixture) **stays in UC** and is merely *referenced*.

## Default selection — an explicit scan that overrides pytest's default

pytest's default is "collect everything under testpaths." duck-test replaces that with an **explicit
default scan**, expressed over a **standard vocabulary of sets** (labels) every project shares and
extends:

    smoke · slow · local · cloud · regression        (+ project-specific)

These are **labels, not a partition** — a test can be `local` *and* `slow`, or `cloud` *and*
`regression`. So the default run is a **set-expression** (e.g. `local and not slow`), applied **only
when you gave no explicit selection**. Any explicit selection (`-m`, `-k`, a path) is respected
verbatim and turns the default scan off — the augment-not-replace relationship you already know from
pytest's `-m`/`-k`.

- **No selection** → apply the default set-expression (the fast local set); deselect the rest (e.g.
  `cloud` → the Databricks set). Announced by the banner.
- **Any selection** → verbatim; no default-scan deselection.

Because these are labels, the mechanism is just **markers**: a tier declaration auto-applies its set
labels as markers to its members, and the default scan is a marker expression. Both controller
(`pytest_configure`) and workers (`collection_modifyitems`) derive the **same** decision from the
**same** original args (`config.option.file_or_dir / keyword / markexpr` — verified identical
everywhere, incl. xdist workers).

**Selection labels vs provisioning resources are different axes.** `cloud` is a *selection* label;
"needs Databricks credentials" is a *provisioning* fact tied to the backend. A tier declaration
carries both, but a set-expression *selects*; a tier's *resources* *provision*.

**Polarity — fail-safe.** A new/untagged test must land in the default run, not be silently excluded.
So write the default as *exclusions* of heavy sets (`not cloud and not slow`), never as an allowlist
of one set — tag only the heavy opt-in sets; everything else runs by default.

## Up-front resource fetch — controller-side, gated

The driver's `pytest_configure` — a **`trylast`** implementation (`@pytest.hookimpl(trylast=True)`
means "run mine *after* other implementations of this hook"; used so your `test/conftest.py` has
*registered* the tiers before the driver *reads* them) — runs on the **controller only** and, before
workers fork:
1. Determine which tiers are **reachable** under the current args (gating, below).
2. For each reachable tier's class-1 credentials: run `fetch` **now, pre-fork, on the controller** (so
   an `op`/biometric prompt lands at invocation, never mid-run) and **publish the block to the store**;
   workers read it via `get_or_provision`, no per-worker `op`. (Supersedes the earlier
   `register_broadcast`/`workerinput` delivery — the store is the uniform carrier; broadcast survives
   only for genuinely-inner values like run-id, if kept at all.)
3. If `validate()` is false → **fail loud + stop, carrying the resource's own message.** Mechanism:
   `pytest.UsageError(error())` → red `ERROR:` + exit 4 (best *visual*, though "usage" is the wrong
   *label* — this is a failed setup, not bad CLI). `pytest.exit(error(), returncode=1)` is the more
   honest "abort the session" but reads less like an error. pytest has no first-class "session
   prerequisite failed" primitive; pick one and be consistent (leaning `UsageError` for the red).
4. If `adopt == "env"` → `os.environ.update(value)`. **`adopt` names *how* a fetched value reaches
   tests.** `"env"` merges the fetched dict (`{DATABRICKS_TOKEN: …}`) into `os.environ`, so three
   consumers see it: the test subprocess (inherits env), the SDK (reads env), and `{DATABRICKS_TOKEN}`
   substitution in the `.test` body. Other modes are conceivable (write a config file, hand to the
   provisioner); `env` is right for Databricks.

A **credential-free run stays possible**: an unreachable tier (bare, `-m 'not databricks'`,
`pytest test/oss_local`) never fetches → no prompt.

### RESOLVED: store-based lazy provisioning defers scan-plan

The tension was: the fetch must happen *before workers fork*, but under xdist the real selected set is
known only *after collection, on the workers* — so an up-front decision could only *predict* from args.
Two things resolve it:

1. **Services go lazy (first-need) via the store** — the controller **needn't know the service set up
   front at all**. This dissolves the need for a scan for *provisioning*, and avoids force-serializing
   fat provisioning ahead of execution.
2. **Credentials stay eager**, fetched at controller pre-fork `pytest_configure` and published to the
   store — the GUI prompt lands at invocation, hard-fail early if unmet. This needs no scan, only a
   *predictive* decision of "is a credentialed tier plausibly in play?" (gating, below).

So **scan-plan is deferred, not needed for the near-term path.** The only things it would still buy are
a *sharper eager-cred decision* (kill the `-k` over-/under-prompt) and *affinity batching* — both
nice-to-haves. A spike confirmed a controller collect-first is *feasible* if we ever want it
(`spike-xdist-collect-first--findings.md`: `pytest_collection` + `perform_collect`, resolves `-k`,
~1-2% overhead) — but the cleaner future form is a **separate foreground scan → provision → exec** whose
xdist run inherits the store address + eager env, *not* an in-controller collect (which fires post-fork,
too late for pre-fork delivery). Revisit only if `-k` over-prompting or affinity batching becomes real.

The residual `-k` imprecision below is therefore **accepted for now** (escape hatch: explicit
deselection, or your own env script). The rest of this section is the predictive eager-cred decision.

**Gating precision** (predictive, from args):
- bare → default tiers (exact).
- path args → tier reachable iff its path **intersects** any `file_or_dir` (`pytest test` → `test` is
  an ancestor of `test/databricks` → databricks reachable → **fetched up front → closes the gap**;
  `pytest test/oss_local` → no intersection → not fetched).
- `-m` → evaluate the tier's marker against the expr with pytest's `Expression` (exact).
- `-k` → **not predictable pre-collection** → punt to the backstop.

**Backstop:** a generic driver `pytest_runtest_setup` — if an item's tier credential `validate()` is
false, `pytest.fail(error(), pytrace=False)` (loud, never a silent skip). Covers the residual `-k`
case + defense-in-depth (creds lapse mid-run). Note the asymmetry it creates (which two-phase would
remove): `-k oss` selects no live test → nothing prompts or fails; but `-k <a-databricks-test>`
selects a live test the controller couldn't predict → no up-front fetch → the backstop only *checks*
(passes iff creds are already in the env, else **fails** — it won't opportunistically `op`-fetch).

## Selected-set banner

Emit from the controller in `pytest_report_header` (decision known pre-collection from args):
```
duck-test tier: smoke (23 selected; 6 databricks deselected — pass a path or -m databricks to include)
```
Header for the decision (always available); count enrichment via `collection_modifyitems` +
terminal summary is nice-to-have (don't block on xdist aggregation).

## Class-2 service lifecycle

Driver gates by the same reachability decision: **start only if the owning tier is active** (the OSS
container must not boot for a databricks-only run); **stop once on the controller** at
`pytest_sessionfinish`; leaks recovered at next session start (see the OSS `reclaim_stale` pattern).
**v0:** the driver owns the *contract* but calls UC's `start_container`/`teardown_shared`; the docker
first-worker-wins lock stays in UC. Promoting a generic `service()` lock into the driver is a
separable later step.

## What moves where

| concern | before (UC) | after |
|---|---|---|
| mark items by tier | subtree conftests | **driver** (auto-marker by path) |
| default selection / deselect | databricks conftest `_no_selection` | **driver** (generalized) |
| up-front class-1 fetch + broadcast + fail | databricks conftest `pytest_configure` | **driver** (trylast, gated) |
| per-test hard-fail backstop | databricks conftest `pytest_runtest_setup` | **driver** (tier-aware) |
| class-2 service start/stop gating | oss conftest `pytest_sessionfinish` | **driver** (start-if-active, controller-stop) |
| banner | — | **driver** |
| creds callables / provisioners / fixtures / docker lock / autouse env | UC | **stays in UC**, referenced |

## Future consumers (API sanity check)

- **iceberg (live)**: a tier with **both** a class-1 credential (REST token) and a class-2 service
  (spark/REST container) — composes with no new concept.
- **benchmark**: opt-in, service-backed, no creds — fits, but wants single-process/stable-timing
  semantics → a future `register_tier(..., solo=True)` run-mode knob (deferred; not a resource).

## Phased implementation plan

- **Phase 0** — API + registry (`register_tier`/`credential`/`service`, `config._duckdb_tiers`); no
  behavior change; self-test vs the stub binary.
- **Phase 1** — selection: driver `collection_modifyitems` auto-marker + default-in deselection.
  Migrate UC (two `register_tier` calls; delete hand-rolled marking + `_no_selection`). Verify
  bare/`-m`/path parity.
- **Phase 2** — up-front class-1 (**closes the gap**): stand up the **store** (`multiprocessing.managers`
  carrier + `get_or_provision`, started at controller `pytest_configure`, address+authkey → env);
  controller (trylast) fetch + gate + `UsageError` + env-adopt, publish the cred block to the store.
  Generic `runtest_setup` backstop. Delete the databricks conftest creds block. Verify `pytest test`
  now fetches on the controller; `pytest test/oss_local` does not.
- **Phase 3** — banner.
- **Phase 4** — class-2 **lazy first-need via the store** (single-flight + configurable timeout;
  replaces the OSS file+`O_EXCL`+reclaim); controller-stop + reclaim-stale. Migrate oss.
- **Phase 5** — optional: declarative ini/TOML tier home; generic driver `service()` lock; benchmark
  `solo` mode; iceberg onboarding as the first external validation; shared `resources` library.

## Open decisions (mostly settled)

Settled by Ben: home = `test/conftest.py`; tiering opt-in (vanilla stays vanilla); banner mandatory;
driver ships a shared `resources` library; registry accepts resources from any locus (consumer /
shared lib / driver). Still to confirm at implementation time:
1. **Polarity** — default-in (recommended).
2. **`-k` handling** — up-front where predictable + clean per-test hard-fail backstop for `-k`
   (recommended). Accept that a `-k`-selected live test hard-fails rather than getting an up-front
   prompt.
3. **Service mechanism ownership** — v0 may keep UC's docker lock, but the target is the driver's
   store-backed single-flight (`get_or_provision`, Phase 4), which retires the file+`O_EXCL` lock.
