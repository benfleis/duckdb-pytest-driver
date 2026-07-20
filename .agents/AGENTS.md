# .agents/ — how this repo's AI-agent config is organized

Self-management doc for the agent-config convention (adapted from
<https://gist.github.com/davidgibsonp/337be9b80b3f03eccd188235c287bb05>). This explains **where
things live**; the actual working rules are in the root **[`AGENTS.md`](../AGENTS.md)**.

## Layout

- **`/AGENTS.md`** — the source of truth: universal, tool-agnostic instructions every agent reads.
  Kept at the repo root as a **real file** (not moved here, not a symlink): `AGENTS.md` at root is the
  portable standard — Codex and others read it directly, and keeping it real avoids tools that don't
  resolve symlinks silently missing the norms.
- **`/.claude/CLAUDE.md`** — Claude Code's entrypoint: a one-line `@../AGENTS.md` import so Claude
  loads the same rules (Claude Code reads `CLAUDE.md`, not `AGENTS.md`). Tracked despite the global
  `**/.claude/` ignore via a `.gitignore` negation.
- **`/.agents/`** — this dir: the generic home for shared, tool-agnostic agent assets. Today just this
  doc; as we add them they live here (reserved, not yet created — no empty scaffold):
  - `skills/` — shared skills (agentskills.io `SKILL.md` spec), symlinked into each tool dir.
  - `mcp/servers.json` — canonical MCP server definitions; a sync script renders per-tool configs.
  - `scripts/` — the symlink/sync automation.

## Principle

**Portable things get symlinked, generated things get a sync script, agent-specific things stay put.**

- *Portable* (identical everywhere) → store once under `.agents/`, symlink from each tool dir.
- *Generated* (same data, per-tool format — e.g. MCP configs) → canonical source under `.agents/` plus
  a sync script that renders `.claude/…`, `.cursor/…`, `.codex/…`.
- *Agent-specific* (no cross-tool equivalent — e.g. `.claude/settings.json`, `.cursor/rules/*.mdc`) →
  leave in the native tool dir.

## Adding a tool

Point the new tool at the root `AGENTS.md` (native read, an `@import`, or a symlink per its docs); put
anything it can share under `.agents/`; keep only its irreducibly-specific bits in its own dir.
