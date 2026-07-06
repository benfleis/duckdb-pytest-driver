# AGENTS.md — norms for AI agents in `duckdb-pytest-driver`

Intent and ground rules for any AI coding agent (Claude Code, Codex, Cursor, Aider, Gemini, …)
working in this repo. This is the **cross-tool source of truth**; `CLAUDE.md` imports it so Claude
Code auto-loads the same rules. If a rule here is wrong or stale, **fix it here**.

## What this is

A generic **pytest front-end over the duckdb `unittest` (Catch2) binary**: collect `.test`
(SQLLogic) files and run them through the binary, plus Python `initialize`/`finalize` drivers,
declarative `@requires` provisioning, managed temp dirs, and batching/parallelism. **The
`.test`/`.sql` file stays the central artifact** — the Python is thin glue, never the test itself.

- **Distribution:** `duckdb-pytest-driver` · **import:** `duckdb_pytest_driver` (+ short `driver` alias).
- **Auto-registered** pytest plugin via a `pytest11` entry point — no `pytest_plugins`, no `sys.path`
  hacks, no symlinks.
- **`duck-test configure`** writes the base `pytest.ini` a plugin can't inject; after it, bare
  `pytest` in a built checkout just works.
- Full design, rationale, and roadmap live in **`docs/`** (`EXTRACT_DRIVER_PLAN.md`, `NOTES.md`,
  `PLAN.md`, `DISPOSITIONS.md`). Read the relevant doc before changing behavior it describes.

## Ground rules

- **Commits are the human's call.** Never `git commit` or `git push` unless told to in the moment.
  Stage changes, show a diff/summary, and wait. Permission to make an edit is not permission to commit it.
- **No secrets in files.** Never write real credentials/tokens/keys into any source, test, or doc —
  use `${ENV_VAR}` placeholders.
- **Verify offline.** The self-tests run against a _stub_ unittest binary (see `tests/`) — no real
  duckdb build, no network. Prefer offline verification; don't invoke wrappers that pop interactive
  credential prompts.
- **Stay dual-mode / import-agnostic.** This code must work both pip-installed **and** vendored into
  duckdb core (`duckdb/test/py/`). Don't hardcode the distribution name in logic; keep imports
  relative; keep the public API in `__init__.py` stable (drivers and provisioners depend on it).
- **Ruff is the linter** (line-length 120; on PATH). Run `ruff check .` before calling work done;
  prefer it over `py_compile`.
- **The tool owns config.** A pytest plugin _cannot_ inject `addopts` / `testpaths` /
  `--import-mode` / `python_files` — those live in what `duck-test configure` writes. Don't try to
  smuggle them into the plugin; extend the CLI instead.
- **Docs discipline.** `docs/` is canonical for design; the README is the skeletal quickstart that
  points at it. Change behavior → update the doc that owns it (and the README if the workflow shifts).
  The env-var _contract_ (`TEMP_DIR`, `{TEST_DIR}`, …) is owned by duckdb's `test/README.md`, not here.

## Layout

```
src/duckdb_pytest_driver/   plugin.py sqllogic.py provision.py requires.py steps.py mnemonic.py cli.py
src/driver/                 compat-alias shim  (`import driver` -> duckdb_pytest_driver)
tests/                      self-tests vs a stub unittest binary (offline)
docs/                       design + roadmap (canonical)
pyproject.toml              hatchling; pytest11 + duck-test entry points; deps (pytest>=7.4, xdist extra)
```

## Dev loop

```bash
uv pip install -e '.[xdist]'         # editable: plain .py edits are live, no reinstall
.venv/bin/python -m pytest tests/    # self-tests (offline, stub binary)
ruff check .
```

Re-run `uv pip install -e` **only** when you touch `pyproject.toml` entry points / deps /
`[project.scripts]` — those are baked into the install at build time. Editing `.py` needs no reinstall.

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
