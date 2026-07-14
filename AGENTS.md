# AGENTS.md — norms for AI agents in `duckdb-pytest-driver`

Intent and ground rules for any AI coding agent (Claude Code, Codex, Cursor, Aider, Gemini, …)
working in this repo. This is the **cross-tool source of truth**; `CLAUDE.md` imports it so Claude
Code auto-loads the same rules. If a rule here is wrong or stale, **fix it here**.

## What this is

A generic **pytest front-end over the duckdb `unittest` (Catch2) binary**: collect `.test`
(SQLLogic) files and run them through the binary, plus Python `initialize`/`finalize` drivers,
declarative `@requires` provisioning, managed temp dirs, and batching/parallelism. **The
`.test`/`.sql` file stays the central artifact** — the Python is thin glue, never the test itself.

- **Distribution:** `duckdb-pytest-driver` · **import + CLI:** `ducktest` (dist name ≠ import name, à la pillow/PIL).
- **Auto-registered** pytest plugin via a `pytest11` entry point — no `pytest_plugins`, no `sys.path`
  hacks, no symlinks.
- **`ducktest configure`** writes the base `pytest.ini` a plugin can't inject; after it, bare
  `pytest` in a built checkout just works.
- Canonical docs: **`README.md`** (use + integrate, worked example), **`docs/ARCHITECTURE.md`** (the
  model — suites, resources, store, provisioning), **`docs/INTERNALS.md`** (hooks/ordering/extending),
  **`docs/PLAN.md`** (roadmap/TODOs). Read the relevant one before changing behavior it describes.

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
  `--import-mode` / `python_files` — those live in what `ducktest configure` writes. Don't try to
  smuggle them into the plugin; extend the CLI instead.
- **Docs discipline.** `README.md` owns "how to use + integrate" (the worked example is its bulk);
  `docs/ARCHITECTURE.md` owns the model; `docs/INTERNALS.md` owns hooks/internals; `docs/PLAN.md` owns
  the roadmap. Change behavior → update the doc that owns it. The env-var _contract_ (`TEMP_DIR`,
  `{TEST_DIR}`, …) is owned by duckdb's `test/README.md`, not here.

## Layout

```
src/ducktest/   plugin.py suites.py store.py provision.py requires.py fixtures.py sqllogic.py sqldef.py steps.py mnemonic.py cli.py
  resources/    ready-made service()/credential() descriptors — azurite.py (see docs/SERVICES.md)
  tools/        rclone.py — object-store seed/clean runner (a means, not core framework surface)
tests/                      self-tests vs a stub unittest binary (offline)
docs/                       design + roadmap (canonical)
pyproject.toml              hatchling; pytest11 + ducktest entry points; deps (pytest>=7.4, xdist extra)
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
