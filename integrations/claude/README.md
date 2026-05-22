# Claude Code Integration

Support level: **Full plugin** — agents, commands, skills, hooks, and MCP server.

## What gets installed

| Component  | Location after install            | Description                               |
| ---------- | --------------------------------- | ----------------------------------------- |
| Plugin     | Claude plugin cache               | Copied from `integrations/claude/plugin/` |
| MCP server | Claude user scope by default      | Wired to `atelier-mcp`   |
| Agents     | Bundled with plugin               | `atelier:code`, `atelier:explore`, …      |
| Commands   | Bundled with plugin               | `atelier status`, `/atelier-context`, …   |
| Skills     | Bundled with plugin               | Auto-trigger on plan/failure/trace        |
| Hooks      | Bundled — **disabled by default** | Opt in via `ATELIER_HOOKS_ENABLED=true`   |

## Install

```bash
make install
```

Use `bash scripts/install_claude.sh --workspace /path/to/workspace` to write a
project-local `.mcp.json` instead of Claude user MCP scope.

The stdio wrapper defaults to the local Atelier HTTP service at
`http://127.0.0.1:8787`, so shared task state and memory live in that service.

Install profile selection:

- `ATELIER_PROFILE=stable` is the default install profile.
- `ATELIER_PROFILE=dev` stages the dev-oriented plugin/instructions.
- `ATELIER_DEV_MODE=1` is still required to expose runtime-gated dev tools.

## Verify

```bash
make verify
```

## Source

Plugin source: `integrations/claude/plugin/`
Full guide: `docs/hosts/claude-code-install.md`
