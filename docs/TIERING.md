# Design: test tiers + up-front resources (driver-owned)

Status: **design, aligned; not yet implemented.** Home for the model behind the smoke/live
split, credential fetching, and shared services. The mechanism lives in **the driver**
(`duckdb_pytest_driver`, the auto-registered plugin) — *not* the `duck-test` CLI, which is only
ingress/config. Everything here works with plain `pytest`.

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

A **tier** = a named subset of the suite with a default-selection policy and a set of up-front
resources. Two orthogonal resource classes:

- **class-1 credential** — fetched *once, up front, on the controller*, broadcast to workers via the
  existing `register_broadcast`/`get_broadcast` seam. Never per-worker, never mid-run (an
  `op`/biometric prompt must land at invocation, not deep in a run).
- **class-2 service** — lazy, *first-worker-wins*, torn down once by the controller (the OSS docker
  container is the model; recovery-at-next-start is the robust net for leaks).

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

## Default selection — "no such thing as bare"

Both controller (`pytest_configure`) and workers (`collection_modifyitems`) compute the **same**
decision from the **same** original args (`config.option.file_or_dir / keyword / markexpr` — verified
identical everywhere, incl. xdist workers):
- **Bare** (no path / `-k` / `-m`) → run default tiers + everything untagged; **deselect** non-default
  tiers (databricks) → fast, credential-free smoke run.
- **Any explicit selection** → no tier-based deselection; pytest's own path/`-k`/`-m` filtering
  decides verbatim (`pytest test/databricks`, `-m databricks`, `-k foo`, `pytest test` = everything).

**Polarity: default-in** (recommended). Tag only the heavy opt-in tiers; the bare-run set is
*everything except* `default=False` members. A new/untagged test stays **visible** on a bare run
(fail-safe). vs default-out (tag the smoke set; new untagged tests silently excluded — fail-dangerous).

## Up-front resource fetch — controller-side, gated, clean-fail

The driver's `pytest_configure` (a `trylast` impl, after the tier declarations register), on the
controller only:
1. Predict which tiers are **reachable** under the current args (gating, below).
2. For each reachable tier's class-1 credentials: `register_broadcast(config, key, fetch)` then force
   `get_broadcast(...)` — runs `fetch` **now, pre-fork, on the controller** (the `op` prompt lands at
   invocation); workers receive it via `workerinput`, no per-worker `op`.
3. If `validate()` false → `pytest.UsageError(error())` → clean red ERROR, zero tests, **stop**,
   carrying the resource's own message.
4. If `adopt=="env"` → `os.environ.update(value)`.

A **credential-free run stays possible**: an unreachable tier (bare, `-m 'not databricks'`,
`pytest test/oss_local`) never fetches → no prompt.

**Gating precision** (controller can't collect, so it predicts from args):
- bare → default tiers (exact).
- path args → tier reachable iff its path **intersects** any `file_or_dir` (so `pytest test` → `test`
  is an ancestor of `test/databricks` → databricks reachable → **fetched up front → closes the gap**;
  `pytest test/oss_local` → no intersection → not fetched).
- `-m` → evaluate the tier's marker against the expr with pytest's `Expression` (exact).
- `-k` → not predictable pre-collection → **punt to the backstop**.

**Backstop:** a generic driver `pytest_runtest_setup` — if an item's tier credential `validate()` is
false, `pytest.fail(error(), pytrace=False)` (loud, never a silent skip). Covers the residual `-k`
case + defense-in-depth (creds lapse mid-run).

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
- **Phase 2** — up-front class-1 (**closes the gap**): controller `pytest_configure` (trylast) fetch
  + gate + `UsageError` + env-adopt; generic `runtest_setup` backstop. Delete the databricks conftest
  creds block. Verify `pytest test` now fetches on the controller; `pytest test/oss_local` does not.
- **Phase 3** — banner.
- **Phase 4** — class-2 gating (start-if-active + controller-stop); migrate oss.
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
3. **Service mechanism ownership** — keep UC's docker lock in v0; generic driver `service()` lock is
   Phase 5.
