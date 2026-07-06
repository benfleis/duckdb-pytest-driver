# NOTES — advanced context (load order, xdist, resources, limits)

Field notes on how pytest + pytest-xdist actually load and run, and what that means
for *where a given piece of provisioning is allowed to happen*. Written after chasing
a "1Password pops up once per worker" bug to its root. Companion to `README.md` (which
covers the driver model itself); this file is the "why the timing is the way it is".

Source pointers are versioned-but-stable; re-verify against the installed packages if
behavior surprises you (`python -c "import xdist,os;print(os.path.dirname(xdist.__file__))"`).

---------------------------------------------------------------------------------
## TL;DR
#

- Conftests load in **two waves**, not one. A conftest loads *early* (before run
  setup) only if you named its directory (or an ancestor) on the CLI; otherwise it
  loads *late*, mid-collection, with its early hooks **replayed**.
- Under xdist, **worker processes are spawned before the tests are collected.** Each
  worker then **re-collects independently** — there is no single shared "list of tests
  + requirements" computed once and handed down.
- Therefore there is **no moment that is both** *after requirements are known* **and**
  *before workers exist*. Those windows do not overlap. (This is the crux.)
- Net rule for provisioning: classify by *when/where it must happen* (below). The one
  category with no clean home inside pytest — **global, once, before any worker** — is
  best done **outside** pytest, in the shell that launches it.

---------------------------------------------------------------------------------
## The naive model, and where it bends
#

A reasonable mental model:

    read .ini -> suck up all conftests -> collect tests (+evaluate @requires)
    -> setup: provision shared stuff -> spin up xdist -> push batches to workers
    -> controller collects results

Three things are wrong, and they matter:

**1. Conftest loading is two waves.**
- `.ini` + conftests on the *invocation path* (rootdir, the dirs you named, and their
  parents) load **first, before run setup**.
- Conftests *deeper* than what you named are discovered by recursion **during
  collection**, and load **then**. pytest replays the historic config hook
  (`pytest_configure`) for a plugin/conftest registered after configure has already
  run — so a late conftest's `pytest_configure` fires *during collection*.
- Consequence: the **same** conftest is "early" or "late" depending on invocation.
  `pytest test/sql/databricks` → that subtree's conftest is early. Bare `pytest` →
  it's late (found by walking the tree).

**2. xdist spawns workers before collection, and each worker re-collects.**
Actual order on the controller:

    read ini + EARLY conftests        # no tests known yet
    pytest_configure                  # controller; still nothing collected
    pytest_sessionstart -> setup_nodes -> makegateway   # WORKERS SPAWNED HERE
    (each worker) re-reads ini+conftests, collects, evaluates @requires  # per process
    controller schedules nodeids -> workers run

  - dsession spawn point: `xdist/dsession.py` `pytest_sessionstart` → `setup_nodes`
    (`xdist/workermanage.py:setup_nodes` → `setup_node` → `makegateway`).
  - The **requirements map is rebuilt per process**, not shipped. There are N+1 of
    them (controller + each worker).

**3. The window you want doesn't exist.**
"Provision shared things, *then* fork" assumes a stage that is both post-collection
and pre-spawn. There isn't one:
  - pre-spawn == config time == nothing collected yet (requirements unknown).
  - post-collection == workers already running.

---------------------------------------------------------------------------------
## Provisioning taxonomy (classify before you wire)
#

| scope | examples | where it lives | notes |
|---|---|---|---|
| **process-local** | spark session, a per-worker container, a *creds check* | lazy + per-process memo, or a fixture | runs inside whatever worker needs it; ordering-immune |
| **per-group** | one shared service for a set of tests | `@pytest.mark.xdist_group("name")` + `--dist=loadgroup` | xdist pins the group to one worker; you must tag it, xdist won't infer |
| **global, once, before any worker** | a 1Password / SSO token | **the shell** (`op run -- pytest`, a make wrapper) | no clean pytest home — see crux above |

Rules of thumb:
- If it can be **memoized within a process**, do that — simplest and ordering-immune.
- If several tests must **share one process-local resource**, give them a common
  `xdist_group` (relies on the default `--dist=loadgroup`).
- If it must exist **once before everything forks**, resolve it in the launching shell
  and let env inheritance carry it. Don't simulate the missing stage inside pytest.

---------------------------------------------------------------------------------
## Controller vs worker
#

- Both run `pytest_configure`. Distinguish them: the **worker** has
  `config.workerinput` set; the **controller / non-xdist main** does not
  (`getattr(config, "workerinput", None) is None`).
- The controller→worker handoff at spawn is `workerinput` (a dict you can populate in
  `pytest_configure_node(node)` on the controller; the worker reads it in its own
  `pytest_configure`). That is the *explicit* channel.
- The *implicit* channel is the **process environment**: workers are plain
  subprocesses. execnet's popen gateway does `Popen(args, stdout=PIPE, stdin=PIPE)`
  with **no `env=`** (`execnet/gateway_io.py`), so a worker inherits the controller's
  `os.environ` **as of spawn time**. Anything you set in `os.environ` *before*
  `sessionstart` propagates; anything set after does not. (Robust, but it is an
  execnet implementation detail, not a documented contract — prefer the shell for
  secrets rather than mutating env in a controller hook to beat the spawn.)

---------------------------------------------------------------------------------
## Output, logging, and capture under xdist
#

Two independent channels with different fates on a worker:

- **Live / real-time** (streaming as it happens — `-s`/`--capture=no`, and pytest's
  *live-log* `--log-cli-level`): **dead on workers.** execnet can't ferry raw
  stdout/stderr in real time, and live-log checks for the terminal reporter (absent on
  a worker) and no-ops. This is why `--steps` forces `-n0`.
- **Captured-on-report** ("Captured stdout/stderr/log call" sections shown on
  failure): **works under xdist.** Captured output is stored on the `TestReport`,
  which is pickled back to the controller and printed under the failed test. Nothing
  to do with real-time fd transfer.

So the xdist-docs "worker stdout/stderr can't transfer" limitation is specifically
about the **live** channel. Captured-on-failure is a different path and is fine.

Practical knobs in this driver:
- `step()` logs at INFO on the `driver` logger.
- The plugin sets `driver` logger → INFO **unconditionally**, so step records are
  *captured* and appear in the **Captured log** of any failing test, **including
  parallel runs** (they ride the pickled report). Passing tests print nothing
  (captured sections only show on failure) → free diagnostics, no noise. Root stays
  WARNING so third-party INFO doesn't flood.
- `--steps` adds the **live** channel (turns on live-log at INFO) and **forces `-n0`**
  (live-log is dead on workers; otherwise it would silently show nothing).

---------------------------------------------------------------------------------
## Consequences already baked into this harness
#

- **OSS UC server** is one container on a fixed name/port → a single-worker resource.
  Tag its tests with a shared `xdist_group` so `--dist=loadgroup` co-locates them on
  one worker; otherwise multiple workers race the fixed name/port. (Per-worker
  isolation = dynamic port + worker-suffixed name; more work.)
- **Databricks rw tests** provision a per-test `cmt__managed__<token>` cell schema, so
  they are **parallel-safe** — no grouping needed.
- **Databricks creds** are run-scoped-before-fork → resolve in the shell
  (`op run -- pytest …` / make wrapper), not via an in-pytest `op` fetch. Fetching
  inside pytest pops 1Password **once per worker** (the memo is per-process) and has
  no clean once-before-fork home (see crux). The per-test hook should only *verify*
  creds + skip-if-absent, never fetch.

---------------------------------------------------------------------------------
## Re-verification one-liners
#

    # xdist install dir
    python -c "import xdist,os;print(os.path.dirname(xdist.__file__))"
    # does the popen gateway pass env=?  (expect: no env= -> inherits)
    grep -n "Popen(" "$(python -c 'import execnet,os;print(os.path.dirname(execnet.__file__))')/gateway_io.py"
    # spawn point (sessionstart -> setup_nodes -> makegateway)
    grep -n "setup_nodes\|makegateway\|pytest_sessionstart" "$(python -c 'import xdist,os;print(os.path.dirname(xdist.__file__))')"/dsession.py "$(python -c 'import xdist,os;print(os.path.dirname(xdist.__file__))')"/workermanage.py
    # live-log enable check (terminal reporter absent on workers -> disabled)
    python -c "import _pytest.logging as L,inspect;print(inspect.getsource(L.LoggingPlugin._log_cli_enabled))"

---------------------------------------------------------------------------------
## Scopes & terminology

Two independent axes. **Execution** = processes and time (where/when a test runs);
**definition** = where a requirement is declared. Don't conflate the identity tokens —
`run-id`, `worker-id`, and `batch-id` all live on the execution axis but at different
layers.

### Execution layers (nested: run ⊃ worker ⊃ batch ⊃ test)

| layer | what | count | token | reused |
|---|---|---|---|---|
| **run** (invocation) | one `pytest` command; one controller process | 1 / run | **run-id** (`timestamp--mnemonic`; computed on the controller, broadcast to workers via `workerinput`) | — |
| **worker** | an xdist process (`gw0`, `gw1`, … under `-n N`) | N / run | **worker-id** | yes — one process for the whole run; runs many batches/tests |
| **batch** | consecutive *driverless* `.test` items packed into one `unittest` subprocess (`--batch-size`) | many / worker | **batch-id** | n/a |
| **test** | one collected item | — | pytest **node-id** | — |

- `run-id ≠ batch-id`: run-id is one per `pytest` invocation; batch-id is one per packed
  group (many per run, many per worker).
- **worker-ids are reused** across batches/tests — a worker is a long-lived process, not
  per-batch.
- **batch-id is never a resource boundary** — it's subprocess packing for driverless tests
  only; paired `.py` drivers skip it (one subprocess per test via `run_paired`). For
  isolation only `run-id` / `worker-id` / `node-id` matter.

**Workers are isolated processes.** No shared memory with other workers or the controller;
comms are structured and boundary-only (controller→worker `workerinput` at startup — how
`run-id` arrives; test commands down / reports up during; `workeroutput` at shutdown). So:
- state a worker builds (env vars, fixture caches, module globals) **persists across that
  worker's batches/tests** and is set **by the worker itself** — nothing propagates it to
  other workers;
- each batch is a fresh subprocess whose env the worker constructs at launch, so a worker
  **can vary context between batches**; *within* one batch the env is fixed for all its tests;
- cross-worker "provision once, everyone shares" needs an **external** channel (filelock,
  server), not in-process sharing.

**Dispatch is dynamic, not pre-queued.** Batch *segmentation* is decided once at collection
(`pytest_collection_modifyitems`); *which worker runs which batch* is decided **dynamically**
during the run — xdist's `load`/`loadgroup` scheduler hands the next unit to whichever worker
is free (`loadgroup` keeps an `xdist_group` whole). Workers execute-and-report, pulling more
as they finish.

### Definition layers (where a requirement is declared)

| layer | declared in | applies to |
|---|---|---|
| **repo / test root** | root `conftest.py` | every test |
| **subdir** (any depth) | e.g. `oss_local/conftest.py` | that subtree |
| **test** | `@requires` on the driver | that one test |

Orthogonal to *where* you declare is **fixture scope** (`function`/`module`/`package`/
`session`) = how long/widely the provisioned resource is shared *within a worker*.

**The bridge:** *where* you declare sets **applicability**; *fixture scope* sets **sharing &
lifetime**. Under xdist, "shared" caps at **per-worker** (no cross-process cache) — truly
once-per-run needs a filelock so one worker provisions and the rest attach. Isolation tokens
follow the grain: per-run → `run-id`; per-worker → `worker-id`; per-test → `node-id` (hashed);
`batch-id` never appears in a resource name.

> e.g. "all oss-uc tests need a server" → a `session`-scoped fixture in `oss_local/conftest.py`
> (subdir definition, per-worker sharing). "write tests need ephemeral tables" → `function`-scoped
> `rw` `@requires` (per-test). "read tests share tables" → `ro` reference (shared, no copy).

**Batching is driverless-only (and requirement-blind) today.** It groups consecutive
`SqlLogicItem`s by binary + working-dir up to `--batch-size`, regardless of requirements; the
binary internally skips any whose `require`/`require-env` is unmet. Paired drivers are never
batched. Grouping *compatible* tests (e.g. two `ro` tests with identical injected env) to share
one setup + one subprocess is a future optimization the requirements model enables — the
criterion is **identical injected env** (`ro` over the same sources qualifies; `rw` with private
per-test namespaces does not).

## Resources & disposition

Everything a test needs is a **resource**: a temp dir, a table (source or copy), a catalog,
credentials, a connection/session. A driver `initialize`/`finalize` is, in the end, just
acquiring and releasing resources. Each resource has two lifecycles:

- **existence:** provision → destroy
- **access:** acquire → release

A resource is specced by one small, flat triple (+ identity, + optional source):

- **acquire-mode** — `shared` | `exclusive`. (This is `ro` | `rw`: an `rw` acquirer dirties
  the resource, so it needs its **own** instance — e.g. a `DEEP CLONE` rather than the source.)
- **create-disposition** — `NEVER_CREATE` | `MAY_CREATE` | `ALWAYS_CREATE` (governs *provision*).
- **destroy-disposition** — `NEVER_DESTROY` | `MAY_DESTROY` (on success) | `ALWAYS_DESTROY`
  (governs *release/destroy*).

One vocabulary covers temp dirs *and* tables *and* catalogs:

| resource | mode | create | destroy |
|---|---|---|---|
| read-only source table | shared | `NEVER` (pre-staged) | `NEVER` |
| read-write copy | exclusive | `ALWAYS` (DEEP CLONE) | `ALWAYS` (drop) |
| per-run temp dir | — | `MAY` | `MAY` (on success) — `--temp-dir-destroy` |
| local catalog (e.g. OSS UC) | shared | `MAY` (stand up if absent) | `MAY` |

**Keep it simple — the discipline:**
1. The spec stays a **flat declaration**; resist a class hierarchy of resource *types*. A table
   and a temp dir share the spec — only the *provisioner* differs.
2. **Don't hand-roll a resource manager.** pytest **fixture scopes** are the provision/share/
   release engine: `session` scope = provision-once-and-share; `function` scope = acquire-per-
   test; teardown = release. You *declare*; fixtures *execute*.
3. Verbs: test-facing **acquire / release**; underneath, provision/destroy are just the two
   disposition axes. Four verbs, no refcounting in your code.
4. **Match duckdb's existing resource words** where they exist (acquire/release, pin/unpin, …)
   so it reads as the same idea, not a parallel one.

**Isolation is the provisioner's choice, not a test knob.** Two ways to keep concurrent /
successive runs from colliding, picked by the *provisioner* (the test declares the same need
either way):

- **by lifecycle** — recreate fresh (`ALWAYS_CREATE`/`ALWAYS_DESTROY`), fixed names. Right when
  ops are cheap (e.g. local OSS UC: tear down service/tables between runs).
- **by namespace** — keep resources, scope them under a per-run name. Right when ops are slow
  (e.g. Databricks: drop everything into `schema = temp_<run-id>` instead of churning).

The **run-id is the shared namespace token**: `BASE/<run-id>` for temp dirs *is*
`temp_<run-id>` for schemas — same per-run id, SQL-safe rendering (`temp_brave_otter`:
underscores, no dashes/colons). And **namespace-via-default-schema** keeps bodies simple: point
the connection's default schema at `temp_<run-id>` and a body's logical `sales` resolves to
`temp_<run-id>.sales` with no name injection — so "fixed names in bodies" and "physical
isolation" coexist for free.

The temp-dir disposition (`--temp-dir-base`, `--temp-dir-destroy {never,on-success,always}`; the
binary's full `--temp-dir-*` / `--run-id` family) is the first concrete instance of this model;
table/catalog provisioning in extension helpers (`test/py/<repo>/`) follows the same spec.


---------------------------------------------------------------------------------
## Reporting & skip homogenization
#

- pytest is the system of record; failure output is **text passthrough** — the binary's
  `SQLLogicTestLogger` writes its diff to `std::cerr`, which survives subprocess capture, so
  the rich diff appears under the failed pytest item unchanged.
- Status model: `pass` / `skip` / `fail` / `internal_error`. Skips come from the binary's stable
  markers (emitted under `--emit-on-skip`): `[SKIP_TEST] <test> :: <reason>` (the whole test was
  skipped) and `[SKIP_TEST_PARTIAL] <test> :: <reason>` (a `mode skip` region; the test ran on).
  One marker per skipped test, so attributable even inside a batch; both collapse to a skip today
  (the `partial` flag is captured for future differentiation), and `pytest_terminal_summary` groups
  them into a counted, by-reason digest.

---------------------------------------------------------------------------------
## Limitations & gotchas
#

- **Stale binary** — a binary built before the `--temp-dir-*` family (or `--emit-on-skip`) errors
  `Unrecognised token: --temp-dir-base`. Rebuild the unittest binary.
- **`${TEMP_DIR_BASE}` substitutes to empty** inside a `.test` (env-refresh timing). Use
  `{TEST_DIR}` (live substitution; `__TEST_DIR__` is its deprecated alias) for now.
- **`python_files=` is off** in `pytest.ini` so pytest doesn't import the many non-pytest
  `test_*.py` scripts in duckdb repos. Consequence: pure-`.py` (py-exclusive) tests aren't
  auto-collected yet; explicit `pytest path/test_x.py` still collects one.
- **Live output is dead on xdist workers** (`-s`, live-log). `--steps` (and `--repl` /
  `--provision-keep`) force `-n0` for this reason; captured-on-failure output still works (see
  "Output … under xdist").

---------------------------------------------------------------------------------
## Environment-variable contract (audit before cutover)
#

The binary sets, for each test: identity — `TEST_NAME`, `TEST_NAME__NO_SLASH` (== `TEST_ID`),
`TEST_UUID`, `RUN_ID`, `TEST_ID`; roots — `WORKING_DIR` / `BUILD_DIR` / `DATA_DIR`; temp —
`TEMP_DIR` (the resolved per-test dir `BASE/[RUN_ID]/[TEST_ID]`), `TEMP_DIR_BASE`,
`TEMP_DIR_ABSOLUTE`, `CATALOG_DIR` (= `TEMP_DIR/TEST_UUID`, not guaranteed to exist). `{TEST_DIR}`
(deprecated alias `__TEST_DIR__`) substitutes to `TEMP_DIR`. See the duckdb `test/README.md` for
the live contract.
