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
  explore.md           atelier:explore — read-only exploration; cyan frame
  review.md            atelier:review — plan/rubric verifier; green frame
  repair.md            atelier:repair — repeated-failure rescue; red frame
  research.md          atelier:research — external research and citation mode
skills/                Slash commands produced via /atelier:<name>
  code/SKILL.md        /atelier:code
  explore/SKILL.md     /atelier:explore
  review/SKILL.md      /atelier:review
  repair/SKILL.md      /atelier:repair
  research/SKILL.md    /atelier:research
hooks/hooks.json       PostToolUse/PreToolUse hooks (all enabled=false by default)
scripts/statusline.sh  Multi-line Claude status chrome; separates `atelier:code` from `atelier`
.mcp.json              MCP server wiring via ${CLAUDE_PLUGIN_ROOT}
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
