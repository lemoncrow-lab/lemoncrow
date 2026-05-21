# Host Integration Matrix

This page summarizes the checked-in install surfaces for each supported host and
the safe operating contract each one gets from Atelier.

## Supported Hosts

| Host | Install path | Interface | Safe default | Enforcement contract | Trace coverage | Unsupported controls | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | `docs/hosts/claude-code-install.md`, `integrations/claude/` | Generated AGENTS surface + MCP wrapper | Keep root entrypoints thin and use imported session traces for recovery | Prompt rules plus MCP tool-mode gating | High once Claude project exports are imported | No guaranteed host-side lifecycle hooks | Use `atelier` CLI and import sessions after execution |
| Codex CLI | `docs/hosts/codex-install.md`, `integrations/codex/` | Generated AGENTS surface + native Codex MCP registry + plugin | Prefer Atelier MCP tools for file I/O, search, edits, and shell work | Prompt rules plus Codex MCP registration and plugin install | Medium-high after importing Codex sessions | No native hook bus or editor task bridge | Use native Codex tools only when Atelier is unavailable or returns `noop` |
| Copilot | `docs/hosts/copilot-install.md`, `integrations/copilot/` | VS Code MCP config + instructions + chat mode + tasks | Treat chat mode + MCP as primary, tasks as shell helpers | MCP visibility plus generated Copilot instructions | Medium; chat traces are strong, shell task traces are weaker | `tasks.json` cannot invoke MCP directly | Use `atelier` CLI tasks and runtime evidence capture |
| opencode | `docs/hosts/opencode-install.md`, `integrations/opencode/` | Generated agent file + MCP wrapper + DB import | Lean on imported sessions when native controls are thin | Prompt rules plus MCP tool-mode gating | Medium after local DB import | No stable hook/event API | Use direct Atelier CLI commands and session import |
| Gemini CLI | `docs/hosts/gemini-cli-install.md`, `integrations/gemini/` | Generated Gemini prompt + MCP wrapper | Prefer explicit verification loops because host controls are thin | Prompt rules plus MCP tool-mode gating | Medium-high after importing saved chats | No rich hook/event contract | Use shell verification and explicit trace recording |
