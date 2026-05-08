# Atelier — Gemini CLI extension

Connects the [Gemini CLI](https://github.com/google-gemini/gemini-cli) to Atelier's MCP server through a formal Gemini extension bundle.

## Install

1. Install the engine:
   ```bash
   cd atelier
   uv sync
   uv run atelier init
   ```
2. Make sure the `atelier-mcp` console script is on your `PATH`, then link the packaged extension from this repo:
   ```bash
   atelier-mcp --help
   gemini extensions validate integrations/gemini/extension
   gemini extensions link integrations/gemini/extension
   ```
3. Restart Gemini CLI. The extension contributes `GEMINI.md`, `commands/`, `skills/`, and the Atelier MCP server in one bundle.

The repo install script automates this flow:

```bash
bash scripts/install_gemini.sh
```

For workspace-scoped activation:

```bash
bash scripts/install_gemini.sh --workspace /path/to/workspace
```

## Verify

```bash
gemini --prompt "List the current Atelier plans and tell me which ones are stale."
```

If Gemini returns the plan list from `<workspace>/.atelier/plans/`, the extension is wired correctly.

## Files

- `extension/gemini-extension.json` — extension manifest.
- `extension/commands/` — bundled Gemini commands.
- `extension/skills/` — bundled agent skills.
- `verify.sh` — non-destructive smoke test (lists plans, prints the first reasoning block).

## See also

- [`../claude/`](../claude/) — Claude Code plugin docs
- [`../codex/`](../codex/) — OpenAI Codex plugin docs
- [`atelier/copilot/`](../copilot/) — GitHub Copilot (VS Code) MCP config
- [`atelier/opencode/`](../opencode/) — OpenCode MCP config

## V2 tools

Atelier V2 adds nine MCP tools (run-ledger, monitor, compress, two
environment helpers, three smart-tool wrappers) on top of the original
six. All V1 tools remain backward compatible. See
[`atelier/codex-plugin/references/v2-tools.md`](../codex-plugin/references/v2-tools.md)
for the full surface.

Gemini CLI extensions can bundle commands, skills, hooks, and MCP servers. The
Atelier extension uses commands and skills for host-native entrypoints and calls
the installed `atelier-mcp` console script for MCP transport, while
CLI-only workflows such as `atelier savings`, `atelier analyze-failures`,
`atelier eval`, and `atelier benchmark` remain available in the shell.
