# AGENTS.md — norms for AI agents in `duckdb-pytest-driver`

Intent and ground rules for any AI coding agent (Claude Code, Codex, Cursor, Aider, Gemini, …)
working in this repo. This is the **cross-tool source of truth**; `CLAUDE.md` imports it so Claude
Code auto-loads the same rules. If a rule here is wrong or stale, **fix it here**.

## What this is

**`ducktest`** (`src/ducktest/`, CLI `ducktest`): a pytest-free runner that runs `.test`
(SQLLogic) files through the duckdb `unittest` binary and Python test bodies, and makes the services,
storages and credentials a repo declares in `ducktest_conf.py`. **The `.test` file stays the central
artifact**; the Python is thin glue, never the test itself.

Canonical docs: **`README.md`** (install, first run), **`docs/design.md`** (how to write a
test: Part 1; how it works: Part 2), **`docs/status.md`** (what works, what is open), and
**`examples/ladder/`** (rungs 00-07, executable docs, checked against `tests/ducktest_goldens/ladder/`
by `tests/test_ladder.py`). Read the relevant one before changing behavior it describes.

## Ground rules

- **Commits are the human's call.** Never `git commit` or `git push` unless told to in the moment.
  Stage changes, show a diff/summary, and wait. Permission to make an edit is not permission to commit it.
- **No secrets in files.** Never write real credentials/tokens/keys into any source, test, or doc —
  use `${ENV_VAR}` placeholders.
- **Verify offline.** The self-tests run against fake binaries (`tests/ducktest_fakes/`) — no real
  duckdb build, no network. Prefer offline verification; don't invoke wrappers that pop interactive
  credential prompts.
- **Keep imports relative** inside the package, and the public API in `__init__.py` stable: the
  `ducktest_conf.py` files and bodies of other repos import it.
- **Ruff is the linter** (line-length 120). Run `ruff check .` before calling work done; prefer it
  over `py_compile`.
- **Docs discipline.** `README.md` owns install and a first run; `docs/design.md` owns the
  model and how to write a test; `docs/status.md` owns what works and what is open; the
  ladder owns one example per idea. Change behavior → update the ladder golden and the doc that
  describes it. The env-var _contract_ of `.test` files (`TEMP_DIR`, `{TEST_DIR}`, …) is owned by
  duckdb's `test/README.md`, not here.

## Layout

```
src/ducktest/      the runner: scan/select/plan/run/report; resources/, engines/, services/, reports/
examples/ladder/    rungs 00-07, executable docs; goldens in tests/ducktest_goldens/ladder/
examples/httpfs/    a conf for duckdb-httpfs on a local S3 server
docs/     design.md, status.md
tests/              self-tests vs fake binaries (offline)
dev/bump-version    release: bump the version, commit, tag
pyproject.toml      hatchling; the ducktest script; deps
```

## Dev loop

Fake binaries; with `DUCKDB_SHELL` set to a built `duckdb` that has `test/unittest` beside it, the
ladder also runs on that build:

```bash
DUCKDB_SHELL=<a duckdb shell> uv run --all-extras --group dev pytest -q
UPDATE_GOLDENS=1 uv run --all-extras --group dev pytest -q tests/test_ladder.py  # re-make the ladder goldens
ruff check .
```

`uv run` runs *in the project's env*: it installs this editable package (so `import ducktest`
resolves) plus the `dev` dependency group (pytest, ruff). Plain `.py` edits are live, no reinstall.
Do **not** use `uvx pytest` / `uv tool run pytest`: that is an isolated env with no project, so every
`import ducktest` fails.

## Conventions

- Match surrounding Python style; keep comments at the density of the file you're editing.
- `.test` files (examples / self-tests): separate logical sections with a labeled header
  (`#` + 77 dashes), and blank-line-separate adjacent `require` statements — the duckdb-wide style.
- Prefer `jq` over `python -m json.tool`.

## Includes

For all files below, load/import if available, do not complain if absent:

- Local agent rules for extended/override context: ./AGENTS.local.md
- Skills are found in .agents/skills/ and .agents/skills-local/

Do ask for user to clarify/update if intentions/rules become contradictory
with multiple sources (and under-specified override intentions).
