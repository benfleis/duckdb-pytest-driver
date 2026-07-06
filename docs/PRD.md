# PRD — pytest as a test front-end for DuckDB extensions

*1-pager. Audience: Iceberg-project collaborators (concept-aligned) and, secondarily,
DuckDB core/leads (to show direction and justify a few small, opt-in runner tweaks).*

## Problem

DuckDB tests today are driven by SQLLogic `.test` files, whose flexibility beyond SQL
offers little setup, fixture support, or test management. Domains like Azure, Iceberg,
and Delta require external setup, non-SQL verifications, and matrix configurations to be
bolted on with brittle ad-hoc scripts. These cause inconsistent test development and
usage in real terms.

## Who it's for

- **Extension developers** (primary) — authoring/running tests for delta, iceberg, azure, …
- **CI** — running those suites in parallel, selectably.
- **Debuggers** — anyone triaging a failure and needing to reach its artifacts fast.

## Why now

A pre-major-release window makes it cheap to land small runner changes before they'd be
breaking. Concretely, the Iceberg / Unity Catalog interop work needs a real test
**matrix** (catalog × engine) now, and there is no good way to express it today.

## Goals / Non-goals

**Goals**
- Per-test Python setup / teardown / verification around SQL tests.
- Declarative requirements and skips; managed temp dirs (local and remote).
- First-class test matrices across catalogs/engines.
- Consolidate the `THIS_THING_IS_PRESENT` env-var sprawl and ad-hoc credential plumbing into
  Python-side, declarative resolution (env / file / secret tools).
- Keep `.test`/`.sql` the central, visible test artifact; run them via the *existing* binary.

**Non-goals**
- Replacing DuckDB-core CI or its `make test` path.
- Rewriting existing `.test` files.
- Reimplementing SQL execution outside the `unittest` binary.

## Alternatives considered

- **(a) Status quo + more bash glue** — what we have; the brittleness *is* the problem.
- **(b) External orchestrator (Dagger / Rust / Go)** — heavy new dependency and language;
  duplicates a test runner; poor fit for per-test Python setup.
- **(c) Extend the C++ `unittest` binary itself** — pushes Python/fixture/matrix logic into
  C++; slow to iterate, wrong language for the job.
- **(d) pytest as a front-end over the existing binary — [chosen].**

## Chosen approach

A thin **pytest front-end** drives the existing `unittest` binary per test, adding Python
setup/teardown, declarative requirements, and matrix configuration on top — while
`.test`/`.sql` files stay the central, reusable artifact and the binary stays the source of
truth for running SQL. It lands in **two tiers**: (1) a **zero-core-change** pytest collector
(pure userland, against today's binary) and (2) **a few small, opt-in, backward-compatible
runner flags** (managed external temp dir; later, a machine-readable skip signal) that make
setup/teardown and matrices first-class. The existing `make test` path is untouched either way.

*Delivered form:* a standalone, pip/uv-installable package — `duckdb-pytest-driver` —
auto-registered as a pytest plugin via a `pytest11` entry point (no in-tree conftest/symlinks).
It stays import-agnostic, so it can also be vendored back into duckdb core if the model changes.

## Success criteria

- `pytest` runs the existing `.test` suite **unchanged**, in parallel, with equal-or-better
  failure output.
- An extension dev adds Python setup/teardown/requirements to a `.test` **without touching C++**.
- The Iceberg/UC interop **matrix** (catalog × engine) is expressible and runnable as
  first-class, selectable tests.
- All core/CI impact is **opt-in and additive**; nothing changes for non-adopters.

## Top risks

- **Subprocess-per-test overhead** → mitigated by batching + xdist parallelism.
- **A parallel runner drifting from core** → mitigated by reusing the binary and keeping the
  collector thin (no SQL-execution logic of its own).
- **Core buy-in for runner flags** → mitigated by opt-in/backward-compatible design and a
  demo-led pitch (the matrix demo, not this doc, is the real argument).
