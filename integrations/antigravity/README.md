# Atelier — Antigravity MCP integration

Connects Antigravity and the `agy` companion CLI to Atelier's MCP server through a checked-in MCP config template and workspace `AGENTS.md` guidance.

## Install

```bash
bash scripts/install_antigravity.sh
```

For a workspace-local install:

```bash
bash scripts/install_antigravity.sh --workspace /path/to/workspace
```

## Verify

```bash
antigravity --version
agy --help
make verify
```

## Files

- `AGENTS.atelier.md` — generated Antigravity-facing workflow surface
- `mcp.atelier.template.json` — workspace MCP template
- `skills/` — optional generated bundle for future host-native packaging

## See also

- [`../claude/`](../claude/) — Claude Code plugin docs
- [`../codex/`](../codex/) — Codex plugin docs
- [`../copilot/`](../copilot/) — Copilot MCP config
- [`../opencode/`](../opencode/) — OpenCode MCP config
