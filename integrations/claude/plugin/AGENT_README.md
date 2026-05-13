# ccrintegrations/claude/plugin/

Canonical Claude Code plugin package for Atelier.

**This is the source of truth for Claude Code integration.**

---

## Structure

```
.claude-plugin/
  plugin.json          Plugin manifest — name=atelier, skills list, no "commands" key
  marketplace.json     Marketplace manifest for plugin-local install (name=atelier)
agents/
  code.md              atelier:code — main coding agent; purple Claude frame
  explore.md           atelier:explore — read-only exploration; yellow frame
  review.md            atelier:review — plan/rubric verifier; green frame
  repair.md            atelier:repair — repeated-failure rescue; orange frame
skills/                Slash commands produced via /atelier:<name>
  status/SKILL.md      /atelier:status
  context/SKILL.md     /atelier:context
  savings/SKILL.md     /atelier:savings
  benchmark/SKILL.md   /atelier:benchmark
  analyze-failures/    /atelier:analyze-failures
  evals/SKILL.md       /atelier:evals
  settings/SKILL.md    /atelier:settings
  atelier-task/        Internal skill — task loop orchestration
  atelier-rescue/      Internal skill — rescue trigger
  atelier-trace/       Internal skill — record outcomes
hooks/hooks.json       PostToolUse/PreToolUse hooks (all enabled=false by default)
scripts/statusline.sh  Multi-line Claude status chrome; separates `atelier:code` from `atelier`
.mcp.json              MCP server wiring via ${CLAUDE_PLUGIN_ROOT}
servers/
  atelier-mcp-wrapper.js  Node wrapper resolving atelier-mcp binary
settings.json          defaultAgent hint
```

## Key Contracts

- `plugin.json` must **not** have a `"commands"` key — that produces `/atelier-name` (dash). Skills produce `/atelier:name` (colon namespace from plugin name).
- `.mcp.json` must reference `${CLAUDE_PLUGIN_ROOT}` (not hardcoded paths) — it is resolved after `claude plugin install` copies the package to `~/.claude/plugins/cache/`.
- All hooks must default to `"enabled": false`.

## Install Paths

| Path                   | Command                                            | Plugin name       |
| ---------------------- | -------------------------------------------------- | ----------------- |
| Standard (recommended) | `make install`                                     | `atelier@atelier` |
| Dev (no install)       | `claude --plugin-dir ./integrations/claude/plugin` | N/A               |

See [docs/hosts/claude-code-install.md](../../docs/hosts/claude-code-install.md) for full guide.

## Verify (no claude CLI required)

```bash
bash scripts/install_claude.sh --print-only
```
