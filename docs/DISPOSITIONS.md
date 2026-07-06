# DISPOSITIONS — the temp dir as `BASE / RUN_ID / TEST_ID` × create × destroy

Design contract for the `--temp-dir-*` unittest flag family (core-bound, alongside opt-in
SKIP-emit). Forward-looking; "what is" lives in NOTES.md.

## Structure: `$BASE / [$RUN_ID] / [$TEST_ID]`

The dir a test writes to is up to three nested levels:

- **`$BASE`** — the root. Default `duckdb_unittest_tempdir` (or `$TMP/…` — see Open), or
  caller-given. Bound literally to env `TEMP_DIR_BASE`.
- **`$RUN_ID`** — per-run isolation, `$TS--$RANDTAG`. Toggle **and specify**: pytest passes
  one fixed id so a run's many `unittest` batches co-locate under a single dir; a plain run
  generates one; a bind-mount turns it off.
- **`$TEST_ID`** — per-test isolation, `$TEST_NAME__NO_SLASH`. Toggle only — isolation, not
  uniqueness, so no randomization (if a collision case ever appears, append `--$NN`). Resolved
  **per-test** (the test name isn't known until a test runs), so it's materialized on the
  per-test path, not at startup.

`TEMP_DIR` (env) = the resolved path = `$BASE[/RUN_ID][/TEST_ID]`. The **service / bind-mount**
case is simply **both toggles off** → `TEMP_DIR == BASE`, no separate "exact" mode needed.

NOTE: This replaces the earlier `{random, exact, stem}` placement enum:
run-isolation and test-isolation are **independent levels, each toggleable** —
not three competing tail styles. Scope maps onto the levels: `$RUN_ID` covers
the run/batch boundary, `$TEST_ID` the test boundary; the service/session
scope (e.g. OSS UC's bind-mount) is BASE-with-both-off.

## create / destroy — one disposition, inherited down every level

- **create** ∈ `{never | on-absent | always}`, default **on-absent** — `never` = must
  pre-exist; `on-absent` = mkdir-p (create any missing level **from `$BASE` down**, never
  clobber); `always` = force-fresh (remove + recreate).
- **destroy** ∈ `{never | on-success | always}`, default **on-success** — `never` = keep;
  `on-success` = remove on pass, **retain on fail** (for inspection); `always` = remove
  regardless.

Dispositions are **uniform at every level — turtles all the way down**: `on-absent` creates
`$BASE` too. Destroy is **recursive, bottom-up: remove only what THIS invocation created,
stopping at the first pre-existing / non-empty level.** Remove the leaf; walk up removing empty
this-run-created ancestors; stop at the first that pre-existed or is non-empty. This is the
long-missing 2nd cleanup step — the outer base is reclaimed iff this run owns it and it's empty;
a pre-existing base is never touched.

## Remote bases — the locality clamp

A remote `$BASE` (`FileSystem::IsRemoteFile` — `s3://`, …): the binary has neither the VFS code
nor credentials to mkdir/rmdir it. So remote **forces create=never + destroy=never**, and levels
are appended as **pure strings, no `mkdir`** — the test owns materialization (object stores
create-on-write). Locality is a clamp **layer** over the dispositions, not a mode.

## Flags (the core surface)

```
--temp-dir-base <path>                        # $BASE; env TEMP_DIR_BASE
--run-id {auto|<id>}                          # $RUN_ID identity (always → RUN_ID env); default auto
--temp-dir-run-id {on|off}                    # is $RUN_ID a TEMP_DIR path level? default on
--temp-dir-test-id {on|off}                   # is $TEST_ID a TEMP_DIR path level? default on ($TEST_NAME__NO_SLASH)
--temp-dir-create {never|on-absent|always}    # default on-absent
--temp-dir-destroy {never|on-success|always}  # default on-success
```

`--run-id` (identity) is split from `--temp-dir-run-id` (path-inclusion): the RUN_ID **value** is
always resolved and exported as the `RUN_ID` env var, whether or not it's a path segment. pytest
passes `--run-id <its-run-id> --temp-dir-run-id off` (identity matches, base already carries it);
a standalone `unittest` generates one and includes it. `$TEST_ID`'s identity is intrinsic (the test
name), so there's no `--test-id` — only its path toggle.

`--test-dir` (the test SCAN/source root → `WORKING_DIR`) is **separate and unchanged** — the old
`--test-dir` / `--test-temp-dir` near-collision is gone. (Flag spelling of the run-id/test-id
knobs is refinable in code; the model is what's locked.)

## Defaults — and what changes for plain `unittest`

Default (no flags): `create=on-absent`, `destroy=on-success`, `RUN_ID=on`, `TEST_ID=on` →
`duckdb_unittest_tempdir / $TS--$RANDTAG / $TEST_NAME__NO_SLASH`. Two intentional behavior
changes vs today's ALWAYS/ALWAYS:

- **TEST_ID=on** → each test gets its own `$TEST_NAME__NO_SLASH` dir (was: a shared pid dir
  cleared between tests). Better isolation; tests hardcoding the shared layout may need review.
- **destroy=on-success** → failed-run dirs are **retained** (was: always deleted). Desired for
  inspection; CI accumulates temp on failures.

## Removed / migrated legacy flags

- **`--test-temp-dir` is removed.** Its must-not-pre-exist guard has no clean disposition
  equivalent, so rather than carry it as a special-case alias, delete it and migrate the only
  two in-tree callers: `scripts/test_zero_initialize.py` → `--temp-dir-destroy never`;
  `.github/workflows/CrossVersion.yml`. (Grep docs / extension repos before deleting a public
  flag.)
- **`--external-test-dir` is unneeded** — it is exactly `--temp-dir-base <b> --run-id <caller-id>
--temp-dir-run-id off --temp-dir-destroy never` (remote clamp automatic). The pytest driver
  migrates to the raw family flags instead of a bundled alias.

## Consumer check — why destroy is an explicit flag

- **`test_zero_initialize.py` (non-pytest):** runs unittest twice (`--one-`/`--zero-initialize`)
  and diffs the two temp trees **post-run** → durable artifacts live **inside** the temp dir →
  needs **`--temp-dir-destroy never`**. This is the concrete consumer that justifies exposing
  destroy as a flag (the default `on-success` would delete the trees before the diff).
- **`test_storage_compatibility.py`:** durable db (`initial_db`) lives **outside** the temp dir,
  self-managed → **indifferent**; default `on-success` is safe. (main's script uses
  `--test-temp-dir` + a `TemporaryDirectory`; v1.5's doesn't — diverged, main canonical.)

## Env vars

Exported to `.test` as `${VAR}`/`{VAR}`: `TEST_NAME`, `TEST_NAME__NO_SLASH` (double `_`, set in
`test/sqlite/test_sqllogictest.cpp`), `TEST_UUID`; `WORKING_DIR`, `BUILD_DIR` (ro), `DATA_DIR`;
`TEMP_DIR` (resolved path), `TEMP_DIR_BASE` (= `$BASE`), `TEMP_DIR_ABSOLUTE`, `CATALOG_DIR`
(= `TEMP_DIR/UUID`); `RUN_ID` (= resolved `$RUN_ID`, `""` when per-run is off), `TEST_ID`
(= `$TEST_ID`, identical to `TEST_NAME__NO_SLASH`'s value — kept alongside it, set in
`test/sqlite/test_sqllogictest.cpp`). No `TEMP_BASE` (a README phantom).

- **Rename:** the v1.5-uncommitted env var `TEST_DIR_BASE` → **`TEMP_DIR_BASE`** (pairs with
  `TEMP_DIR`; cheap while uncommitted).
- **Timing fix:** `UpdateEnvironment()` runs today inside `Initialize()` **before** arg-parse, so
  `--temp-dir-*` context isn't reflected unless cwd changes. Move it — keep `Initialize()`
  setting inputs (`working_dir`, `test_uuid`) early, but call `UpdateEnvironment()` **once after
  `PrepareTempDir()`**, when all context is final. Safe: `test_env` is consumed only at test-run
  time (inside `Catch::Session().run()`).

> ⚠️ **Reconcile hazard:** `TEMP_DIR_ABSOLUTE` (on **main**, commit `55825014e61`) and
> `TEST_DIR_BASE` / `--external-test-dir` (**uncommitted on v1.5**) live on divergent commits —
> neither has both. Evening-up must **UNION**: both lines get both env vars.

## core vs driver

- **core (C++):** the flag family + `PrepareTempDir` / `DestroyTempDir` executors (create/destroy
  inheritance, recursive bottom-up reclaim), the remote clamp, opt-in SKIP-emit. The binary
  **executes** dispositions; it holds no policy. This deliberately revises the earlier "binary
  owns no lifecycle" stance — the core surface grows from path+placement to the full family,
  justified by the `zero_init` consumer that needs `--temp-dir-destroy never` directly.
- **driver (pytest):** **decides** — scope, run-id, which dispositions apply — and passes flags.

## Open

- **`$TMP/duckdb-test-temp` default base** — separable / optional. Bonus: an absolute default
  makes `TEMP_DIR == TEMP_DIR_ABSOLUTE`, partly mooting the divergence. Caveat: breaks anything
  assuming the relative under-cwd dir.
- **Scope model** — map `create × destroy × scope` onto pytest fixture scopes + the `@requires`
  resource model (one vocabulary, not two). The broader resource lifecycle (tables, catalogs,
  service-sessions) and acquire-mode (shared/exclusive = ro/rw) generalize the same triple.
- Confirm reviewers accept the widened core surface (family + executors) before landing.
