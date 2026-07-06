# Design (v0-dev): collection trust (scan-reconcile) & labeled configs (`--profile`)

Two designs feeding the "**run pytest on all my repos with 100% trust it's correct**" goal and the
delta/iceberg config need. Pre-iceberg structural doc — get these shapes right before new backends
land. Roadmap bullets live in `PLAN.md`; this is the fleshed-out design.

## 1. Scan-reconcile — make collection authoritative

### Problem
Collection today is **filesystem-only**: pytest walks `test/`, `pytest_collect_file` turns each
`.test` into a run. The binary's registration is *different and authoritative*:

- It registers `test/**` `.test` + `.test_slow` + `.test_coverage`, **plus**
  `third_party/sqllogictest/test` (minus an excludes list), **plus** each loaded extension's `_deps`
  test paths.
- ⇒ **(A) binary-has / pytest-misses** — silent false-green: `.test_slow`/`.test_coverage`,
  third-party sqllogic, extension `_deps` tests never collected.
- ⇒ **(B) pytest-has / binary-excludes** — pytest hands the binary a name it skips or errors on.

"pytest green" ≠ "the binary's known set passed" until these agree. That gap *is* the trust blocker.

### Approach — add `unittest -l` as a second gather source and reconcile
- `unittest --list-test-names-only` = the authoritative name set for the resolved binary + its loaded
  extensions, scoped to selection.
- Two reconcile modes (ship **verify** first, evolve to **authoritative**):
  - **verify** (cheap, high-trust-per-effort): keep FS-driven collection, but at session start diff
    FS-collected vs `unittest -l`; **hard-error** on any divergence, printing the delta
    (missing-from-pytest / unknown-to-binary). Turns silent gaps into loud failures with minimal
    machinery.
  - **authoritative** (target): drive collection from `binary-list ∪ FS-scan`, deduped by test name
    (the existing nodeid dedup in `pytest_collection_modifyitems` is the seed). FS scan is still
    needed for `.py` drivers + node mapping.
- Fold in **suffix expansion**: recognize `.test_slow`/`.test_coverage` as bodies, gated by opt-in
  (`--slow` flag / `slow` marker) so default runs stay fast. This is where **segmentation / batch
  ordering** lands — slow tests are batch-1 and ordering matters.

### Depends on / notes
- Resolve the binary **once** at `pytest_configure` (session invariant) before listing — see PLAN's
  "`--build auto` fail-fast on ambiguity" bug; scan-reconcile can't run per-collection-root.
- Extension `_deps` tests only appear when the extension is loaded → the list is *binary-specific*;
  reconcile per resolved binary.
- Related: the **dual-scoping** guard (below) is the FS-side half of the same trust concern.

### Dual-scoping guard (folds in here)
Collection scope is governed by **two independent settings**: pytest's `testpaths` (ini, controls
which dirs pytest *walks*) and the plugin's `test_root` (controls which walked files it *accepts*).
They agree in the default setup (`testpaths=test`, `test_root=<wd>/test`) but can silently diverge:
- `testpaths` **narrower** than `test_root` (e.g. `testpaths=test/sql`) → `.test` files elsewhere
  under `test/` are **never walked** — silent under-collection (you think "all of test/" ran).
- `testpaths` **broader**, or `--duckdb-test-root` pointed outside `testpaths` → wasted walking /
  nothing collected.
Nothing crashes — it's quiet mis-collection, exactly what erodes trust. **Fix:** at
`pytest_configure`, validate `test_root` is consistent with `testpaths` (or derive one from the
other) and error on divergence.

## 2. Labeled configs — `--profile`

### Problem
The binary's `--test-config` (JSON) is too deep/low-level to be the user-facing knob, and there's no
*named, reusable* notion. Delta wants its minio / S3 / GitHub-workflow configs as selectable pytest
options; iceberg will want its own. Today the only seam is `--unittest-args` (raw passthrough) +
per-consumer conftest defaults — unlabeled and unshareable.

### Approach — a profile is a named bundle resolved *above* the binary
A **profile** composes, under one name:
- **env** vars (exported for the run / merged into the body's `${...}` substitution),
- **`--unittest-args`** additions (e.g. `--test-config <path>`, `--skip-error-messages`),
- **provisioner selection + params** (which backend `@requires` uses, and its params).

Surface: `pytest --profile <name>` (single for v0; `--profile ?` lists available). Registry home —
two candidates, pick per case:
- **conftest** `register_profile(name, …)` (mirrors `register_provisioner`) — when a profile needs
  logic; OR
- **declarative** file (`.agents/`-style `profiles.toml`, or an index over `test/configs/*.json`) —
  when it's pure data, for shareability across repos.

Resolution: `--profile` applies its bundle, still overridable by explicit `--unittest-args`/env.

### Why at the pytest layer, not the binary
A profile spans env + args + provisioner + creds — cross-cutting concerns the binary's config can't
express. One knob (`--profile minio`) drives config *and* provisioning *and* creds together, and
profiles become discoverable. Keep **markers** (select: `-m slow`) distinct from **profiles**
(configure) — see PLAN open-Q 8.

### Open
- Registry home: conftest calls vs declarative file (lean declarative for shareability).
- Interaction with `@requires` / matrix cells (a profile may pin an axis).
- OSS-UC vs Databricks-UC: the near-term forcing function — unify their mechanisms so one profile
  shape covers both (per the UC port goal), with `@requires` + parameterized-manual as the escape
  hatch only where genuinely needed.
