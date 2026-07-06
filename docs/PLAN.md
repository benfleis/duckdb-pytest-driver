# PLAN — roadmap, TODOs, open questions

Forward-looking only; "what is" lives in README.md / NOTES.md. Problem statement & pitch:
`PRD.md` at the pytest repo root.

## Status snapshot

**Built & in use**

- pytest collector over the unittest binary: the `.test` lane (collect / batch / invoke /
  parse), xdist (`-n`, `--dist=loadgroup` + per-batch `xdist_group`), `--batch-size`.
- Skip reporting: binary emits `[DUCKDB_SKIP] <test> :: <reason>`; collector → status +
  per-reason digest (works batched).
- `--external-test-dir` + `--external-test-dir-destroy {never,on-success,always}`; per-run id
  (`timestamp--mnemonic`), cleanup-on-success.
- `--build {auto,debug,release,reldebug,relassert,latest}` binary resolution;
  `--repl` (interactive shell for one selected test) + `--provision-keep` /
  `--provision-dry-run`; `--steps` live step logging.
- SoT-by-symlink consumption (`conftest.py`, `pytest.ini`, `test/py/driver`); `@requires` +
  `resources` fixture + provisioner registry; UC scaffold (`test/py/uc`).
- Module split: `driver.sqllogic` (the `.test` lane) vs `driver.plugin` (harness / hooks).

**Pending / to verify**

- **Reported-unit flip** — a driver `.py` is still the reported unit; flip so the **body**
  (`.test`) is reported and the `.py` only contributes hooks. _(pending)_
- **`--repl` python shell** — currently duckdb only; a python shell for pure-`.py` bodies is
  planned (repl-kind = body-kind). _(planned)_
- **uuid → `timestamp-mnemonic` / per-invocation → per-test** temp subdir — C++ change
  unbuilt; today the binary appends a per-invocation `<uuid>`. _(unbuilt)_
- **py-exclusive lane** — `python_files=` is off (drop-in robustness); re-scope to let pure
  `.py` tests collect in a normal run. _(pending)_
- `${TEST_DIR_BASE}` substitutes empty in a `.test` (env-refresh timing); `__TEST_DIR__`
  (live) works — pick a live token / general var-injection channel. _(open)_
- **`.test_slow` files are silently ignored** — the collector matches the suffix `.test`
  exactly (`pytest_collect_file`: `file_path.suffix != ".test"`), so `_slow`-tagged bodies
  (`*.test_slow`, e.g. `tpch.test_slow`, `tpcds.test_slow`) are never collected or run.
  Intentional for now (keeps the slow TPC suites out of the default run), but needs fixing:
  recognize `*.test_slow` as a body too, gated behind an opt-in (a `--slow` flag and/or a
  `slow` marker so they run only when asked). _(known gap)_; additionally need to
  design/handle tags like slow

## v0-dev sprints (post-commit, near-term)

Design detail for the first two: **`DESIGN-v0-collection-and-profiles.md`**.

1. **Collection trust — scan-reconcile** _(the "100% trust" blocker)_. Collection is FS-only today;
   the binary's registered set (via `unittest -l`) differs — `.test_slow`/`.test_coverage`,
   `third_party/sqllogictest`, extension `_deps` are silently uncollected (false-green). Add
   `unittest -l` as a second gather source: ship **verify** (diff FS vs binary, hard-error on
   divergence) first, evolve to **authoritative** (collect from `binary ∪ FS`, dedup by name).
   Requires resolving the binary once at `pytest_configure` (see the fail-fast bug below).
2. **Slow/coverage + segmentation** _(part of the trust sprint)_. Recognize `.test_slow`/
   `.test_coverage` as bodies behind `--slow`/`slow` marker; slow tests are batch-1 → the
   batch-ordering / segmentation work lands here.
3. **Dual-scoping guard**. `testpaths` (ini, what pytest walks) vs `test_root` (plugin, what it
   accepts) can silently diverge → under/over-collection. Validate/derive at `pytest_configure`,
   error on mismatch. (FS-side half of #1.)
4. **`.sql` body lane** _(planned, marked in code)_. Extend the collector gate + `_stem_path`
   pairing once the lane is real; docstrings currently mark it "`.sql` planned".
5. **Labeled configs — `--profile`**. Named bundles (env + `--unittest-args` + provisioner
   selection) resolved above the binary; delta (minio/s3/gh-workflows) + iceberg need it. Registry
   in conftest (`register_profile`) or a declarative file. Design in the doc above.

## Roadmap (future)

- **Matrix** — one body × N cells (catalog/engine), via pytest `parametrize` / scoped
  fixtures / `pytest_generate_tests`. Cells are batch-1 and likely imply `slow`. Per-cell
  variable injection into the body is the open mechanism (the default-schema trick covers many
  cases).
- **`.cpp` lane** — `--cpp` + `unittest -l` as a second gather source, deduped against the FS
  scan. Deferrable.
- **Cutover** — per-extension; `.test` files stay until each extension is stable on pytest.
- **Credentials** — env-first; resolve secrets in the launching shell (`op run -- pytest …`),
  not inside pytest (see NOTES "global, once, before any worker"). The per-test hook only
  _verifies_ + skips.
  - _[debug]_ `op` prompted REPEATEDLY when fetched in-pytest — it should cache the authN and
    ask once itself. Root-cause before any re-introduction (likely each xdist worker process
    invokes `op` with no shared session; once-before-fork is the fix).
  - _[reconsider]_ re-inject an in-pytest creds fetch — IF `op` caches correctly (one prompt),
    auto-fetch simplifies UX (bare `pytest` "just works" for databricks) without the popup
    storm. Gated on the debug above.
- **Seed reuse** — standardize the OSS seed on the convention table `id_name (id INT, name
STRING)` (+ `tpc{h,ds}` for bulk reads); avoid bespoke per-test tables so provisioning stays
  one shape. (The checkpoint port reuses `id_name`.)
- **`--build auto` should fail FAST on ambiguity** _[bug]_ — with multiple binaries built (e.g.
  debug + release), `--build auto` raises `pytest.UsageError("multiple unittest binaries …
  ambiguous")` LAZILY per collection root, so pytest emits one ERROR per root (`test/common`,
  `test/extension`, `test/fuzzer`, …) and presses on instead of bailing. Resolve the binary ONCE
  at `pytest_configure` (session level) and error there, so an ambiguous / missing binary halts
  the whole session immediately with a single clear message (same for `--unittest-binary` not
  found). Binary resolution is a session invariant, not a per-item decision.

## C++ queued (the opt-in runner changes)

- **per-test OUTCOME emit (`--emit-on-test`, its own flag)** — successor to the skip markers /
  the abandoned per-file statement histogram. Emit ONE line per Catch test-case (= per `.test`
  for sqllogic, + each C++ `TEST_CASE`): `[TEST_RESULT] <name> :: pass|fail|skip|partial[ :: <reason>]`.
  Two emit points: sqllogic already has full per-test state in `test_sqllogictest.cpp` `testRunner`
  (`error.HasError()` / `skipTestDuringRun` / `AddSkipReason`); a uniform hook for ALL tests is a
  Catch `EventListenerBase::testCaseEnded(TestCaseStats)` (none exists today; a bare listener can't
  see `partial` — that's sqllogic-layer). **Payoff:** authoritative pass/fail per test in ONE batch
  pass ⇒ driver drops the returncode + individual re-run attribution; and subsumes
  `[SKIP_TEST]`/`[SKIP_TEST_PARTIAL]`. **Outcome set is exactly {pass,fail,skip,partial}** given the
  suite. pytest mapping: pass→passed, fail→failed, skip(+reason)→`pytest.skip`, **partial→passed +
  `warnings.warn(SkipWarning)`** (there is NO native "passed-with-skip" outcome — warning is the
  idiomatic home; a custom `pytest_report_teststatus` letter is the alt). NOT available/worth it:
  xfail/xpass (Catch has `[!mayfail]`/`[!shouldfail]` but duckdb uses **0**), and `error` (a
  pytest/driver fixture-setup concept, not something the binary emits). _(deferred; ship the partial
  markers first, revisit if the batch-attribution win is worth the listener.)_
- **skip markers, done** — `[SKIP_TEST]` (whole, Catch aborts) vs `[SKIP_TEST_PARTIAL]` (region
  `mode skip`, test runs on); `PrintSkip(..., bool partial)`; driver `_scan_batch_skips` regex
  recognizes both, records `{reason, partial}`, collapses both to skip today (partial flag ready).
  On `emit-on-skip` (C++) + driver; `manage-temp-dirs` / image-tests need the restack/sync.
- per-test-stem subdir (flaggable) + a "use-my-path-as-is / don't-append-uuid" mode.
- port the SKIP marker + dir flag to core — the **only two C++ essentials** (the rest of the
  driver is moving to a SEPARATE repo, so these are the permanent core surface). Two
  independent change-sets:
  - **SKIP** — make it **opt-in** (`--emit-on-skip`, gating the shadow `SKIP_TEST` + `PrintSkip`
    → adds an arg to `unittest.cpp`) and **rename `[DUCKDB_SKIP]` → `[SKIP_TEST]`** (matches the
    Catch2 `SKIP_TEST` macro already shadowed in `runner`/`result_helper`; no other precedent in
    our/Catch2 output; generic name for the repo split). Then push. Files:
    `sqllogic_test_logger`/`runner`/`result_helper` + `unittest.cpp`. _(applied to v1.5-variegata
    + image-tests/duckdb + driver; awaiting build/verify/PR + dup to main.)_
  - **empty-list test-config quirk** _(minor, optional core fix)_ — UC sets HTTP/network errors
    fatal by overriding the binary default `skip_error_messages = {HTTP, Unable to connect}` with
    an empty set. CLI `--skip-error-messages '[]'` works; a `--test-config` JSON works too but
    ONLY as the **string** `"skip_error_messages": "[]"` — the empty **array** `[]` flattens to
    `''` in `LoadConfig` (`json->Flatten()` stringifies values) and crashes the `VARCHAR→VARCHAR[]`
    cast. Non-empty arrays are fine. UC uses the string workaround (`test/configs/uc.json`); a core
    fix would let the natural `[]` array load. No PR strictly needed.
  - **dir flag** — **HOLD.** `--external-test-dir` == create=never/destroy=never + `random`
    placement; finalize the placement set (`random`/`exact`/`stem`) + the create×destroy×scope
    contract first so the merged C++ surface is final. See **DISPOSITIONS.md**.
- (optional) move the destroy-on-success disposition down into the binary.
- **WAIT / debugger-attach primitive** — a way to pause a specific executing test (a `.test`
  directive or runner flag: wait-on-signal / sleep / wait-for-stdin) so `lldb` can attach to
  the right unittest subprocess mid-test. Tractable because each paired test already runs in
  its own subprocess; needs to target exactly ONE test.

## Driver interface (Fork A — being designed)

- Declarative `@requires(...)` (static metadata: `source` / `access` / `commit` / `storage` /
  `name`) + `initialize` / `run` / `finalize` hooks + a `Context` (`spark`, `table_fq_name`,
  `table.DROP(...)`, per-cell vars). The resource model — acquire-mode × create/destroy
  disposition — is in NOTES.
- The `Context` surface is **not yet specified** — await the external uses doc.
- Matrix maps onto pytest parametrize; **isolation = lifecycle** (cheap / OSS) vs
  **namespace** (slow / DB), the provisioner's choice; the run-id is the shared namespace token.
- Lifecycle naming is `initialize` / `run` / `finalize` (not setup/teardown — hooks may
  assert); two layers (cell ⊃ test); ordering is LIFO.
- **Cell-schema granularity — per-RUN (documented) vs per-TEST (implemented).** NOTES says the
  run-id IS the namespace (`temp_<run-id>`, shared), but `_provision_token` appends a per-nodeid
  `sha1[:6]` → per-TEST schemas (`temp_<run-id>_<hash>`). That hash is currently **load-bearing**:
  the `@requires` lane is NOT batched (batching is sqllogic-only + size-based) and has no dep-aware
  scheduling, so the per-test name is the sole cross-worker collision guard. **Dep-aware
  provisioning** (`xdist_group`-by-dep, or a disjoint-name discipline within a run) would let the
  run-path token collapse to the documented per-run form. `--repl` needn't carry the hash
  regardless (single selected test) — a cheap, safe cleanup independent of this decision.

## Open questions

1. `Context` surface + the generator/checker/`@requires` helper-library shape.
2. Driver load timing (eager vs lazy) — gated by whether static `@requires` informs batching.
3. Per-cell variable injection into the body (matrix) — default-schema vs explicit.
4. Catalog as one matrix axis spanning managed + cloud, or separate suites (creds).
5. Cross-engine interleaved sequences: `.test`-central-with-py-steps vs py-exclusive.
6. Implicit `.py` semantics (what the do-nothing default driver provides).
7. `delta/PLAN-gen.md` — data-generation story; reconcile generators reused vs reimplemented.
8. **Tags/params in `.test` files** — pytest **markers** are the canonical select/config layer
   (`-m "slow and not flaky"`; markers double as config via `get_closest_marker`). `.py`-driven
   tests map cleanly; the open case is `.test` bodies with NO `.py` driver, which would need
   header directives (`# tags:`, more `# requires:`) bridged to markers at collection (as we
   already do for `# group:`/`xdist_group`). **Deferred on purpose:** `.test_slow` filename
   suffix is the stopgap; keep `.test` files as-is and let the real forcing function — a repo
   that declines `.py` drivers — drive adding `.test` header metadata, not imagined need.
   **Cost note (at scale):** in duckdb core (thousands of `.test` files) filtering by FILENAME
   is ~free from the FS walk, while header tags require opening + reading every file's first
   block. So filename encoding is the right lever for tags that gate COLLECTION (`slow` = don't
   even collect by default); header directives suit richer metadata read only once a file is in
   scope. Likely filename-for-collect-gates + headers-for-config, not one replacing the other.
