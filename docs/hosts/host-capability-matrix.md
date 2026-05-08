# Host Capability Matrix And Enforcement Contract

This matrix reflects currently available host capabilities as of 2026-05-02,
validated against upstream host docs and Atelier's integration scripts.

Atelier ships one runtime, but each host exposes a different control surface. The contract below
separates integration from enforcement so the project does not claim identical plugin behavior where
the host only supports advisory MCP/instruction workflows.

## Enforcement Levels

| Level               | Meaning                                                                                                  |
| ------------------- | -------------------------------------------------------------------------------------------------------- |
| `advisory`          | Atelier returns guidance or route decisions; the host/user remains responsible for following them.       |
| `hook_enforced`     | Host hooks can warn, block, or inject context around native tool events.                                 |
| `wrapper_enforced`  | An Atelier wrapper can gate task start, completion, or model flags before invoking the host CLI.         |
| `provider_enforced` | Atelier owns the model call path. Future-only unless a provider execution adapter is explicitly enabled. |

### RouteDecision vs RouteExecutionContract

A `RouteDecision` is a _decision artifact_: it records the chosen tier, confidence, reason, and
required verifiers for a single step. A `RouteExecutionContract` is a _host descriptor_: it states
whether the host can _enforce_ that decision (block start, require verification) or can only _advise_
on it.

Use the `route` MCP tool (`op="decide"`) to retrieve host routing decisions and enforcement facts.

## Trace Confidence Levels

| Level          | Meaning                                                                                        |
| -------------- | ---------------------------------------------------------------------------------------------- |
| `full_live`    | Live hooks record prompt, tool, edit, command, compact, and stop events.                       |
| `mcp_live`     | Atelier MCP calls and tool outputs are captured; native host edits/commands may be incomplete. |
| `wrapper_live` | Wrapper captures task start/end and validation results, but not every native host event.       |
| `imported`     | Host session data is imported after the run.                                                   |
| `manual`       | Agent must call `trace` with observable facts.                                                 |

### Trace Metadata Fields

Every trace records four evidence fields that proof reports must expose:

- `host` — Host label derived from the agent string (e.g. `claude`, `codex`, `copilot`, `opencode`, `gemini`).
- `trace_confidence` — One of the five levels above.
- `capture_sources` — Active evidence channels (e.g. `["hooks", "mcp"]`).
- `missing_surfaces` — Host surfaces not captured (e.g. `["bash_outputs", "file_edits"]`).

See `docs/engineering/trace-confidence.md` for the full specification.

## Capability Matrix

| Host            | Native surfaces Atelier uses                                    | MCP                      | Hooks / events                                          | Wrapper               | Routing enforcement                                            | Trace confidence                                               | Unsupported controls                                                                           | Fallback                                          |
| --------------- | --------------------------------------------------------------- | ------------------------ | ------------------------------------------------------- | --------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| Claude Code     | Plugin package, commands, agents, skills, hooks, MCP            | Yes (stdio)              | Yes; hooks can warn/block/inject when enabled           | Optional              | `hook_enforced` with hooks, otherwise `advisory`               | `full_live` with hooks, otherwise `mcp_live`                   | `provider_enforced` (future-only, disabled), full model-provider override outside host surface | MCP-only mode with manual trace reminder          |
| Codex CLI       | Plugin package, marketplace, AGENTS.md, task templates, wrapper | Yes (stdio + HTTP)       | Limited compared with Claude hooks                      | Yes (`atelier-codex`) | `wrapper_enforced` for wrapper runs, otherwise `advisory`      | `mcp_live` + `wrapper_live`; imported sessions where available | `hook_enforced` parity with Claude hooks, `provider_enforced` (future-only, disabled)          | Native Codex flow with MCP tools and manual trace |
| VS Code Copilot | Workspace MCP, instructions, chat mode, VS Code tasks           | Yes (`.vscode/mcp.json`) | VS Code tasks/instructions; no Claude-style hook parity | Yes (task shortcuts)  | `advisory`                                                     | `mcp_live`; task outputs/manual trace for native chat edits    | Host-level hard blocking of model/tool calls, `provider_enforced` (future-only, disabled)      | Workspace instructions plus explicit MCP calls    |
| opencode        | MCP config, agent profile, commands                             | Yes                      | Host-specific command/profile hooks only                | Yes                   | `wrapper_enforced` where wrapper is used, otherwise `advisory` | `mcp_live` + imported/manual trace                             | Cross-host hook parity and provider-owned execution (`provider_enforced`) disabled             | MCP-first workflow with agent profile             |
| Gemini CLI      | Extension package, command presets, skills, GEMINI context file | Yes (stdio/SSE/HTTP)     | Host-specific command presets                           | Yes                   | `advisory` or `wrapper_enforced` when launched through wrapper | `mcp_live` + imported/manual trace                             | Host-native hard enforcement beyond wrapper, `provider_enforced` (future-only, disabled)       | Extension-native MCP workflow                     |

## Notes By Host

### Claude Code

- Native plugin path is first-class; Atelier ships a Claude plugin package under `integrations/claude/plugin/`.
- Hooks are available but should remain opt-in for safety. When disabled, routing and trace
  capture downgrade to MCP-only advisory behavior.
- MCP-only mode is a fallback, not the recommended path.

### Codex CLI

- Codex now supports Atelier through a packaged plugin and marketplace entry rather than only copied skills.
- The installer materializes a local plugin source and generated MCP wrapper, then points the marketplace at that source.
- Atelier adds a wrapper-style workflow to enforce reasoning preflight consistently.

### VS Code Copilot

- VS Code supports MCP servers in workspace/user `mcp.json` and exposes MCP tools/prompts in chat.
- Copilot integration quality is highest when combining MCP + chat mode + task shortcuts.
- Routing decisions are advisory unless the user runs a task/wrapper that can enforce the gate.

### opencode

- opencode supports MCP, agents, commands, and plugin ecosystem docs.
- Atelier uses workspace-local MCP config and an explicit Atelier agent profile for predictable behavior.

### Gemini CLI

- Gemini CLI supports extensions that can bundle MCP servers, commands, skills, and context.
- Atelier uses a linked extension bundle instead of a raw settings merge.

## Install Surface Summary

- Unified installer: `make install`
- Unified verification: `make verify`
- Per-host installers remain available as `scripts/install_<host>.sh` for advanced or dry-run workflows.

## Host-Specific Limitations

- Claude Code hooks can be strict; keep disabled by default unless workflow maturity requires enforcement.
- Codex wrapper pattern is additive; native Codex subagent/skills flow remains available.
- Copilot behavior depends on VS Code version and MCP trust state, and native chat edits may require
  manual or imported trace evidence.
- opencode and Gemini configurations vary between user-global and project-local scopes.
- `provider_enforced` is a contract placeholder only. It remains future-only and disabled in this
  packet unless a later provider execution packet explicitly enables it.
