# Host Integration Matrix

## Supported Hosts

| Host            | Install path                    | Interface | Safe default | Enforcement contract                                           | Trace coverage                                          | Unsupported controls                                                                | Fallback                                              |
| --------------- | ------------------------------- | --------- | ------------ | -------------------------------------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **Claude Code** | MCP + skills + agents + hooks   | MCP stdio | `suggest`    | `hook_enforced` when hooks are enabled, otherwise `advisory`   | `full_live` with hooks, otherwise `mcp_live`            | `provider_enforced` (future-only disabled)                                          | MCP-only mode + explicit `trace`                      |
| **Codex CLI**   | MCP + AGENTS.md + wrapper       | MCP stdio | `suggest`    | `wrapper_enforced` for wrapper runs, otherwise `advisory`      | `mcp_live` + `wrapper_live`; imported sessions optional | Claude-style hook parity, `provider_enforced` (future-only disabled)                | Native Codex flow + MCP + manual trace                |
| **Copilot**     | MCP + instructions + chat mode  | MCP stdio | `suggest`    | `advisory`                                                     | `mcp_live`; tasks/manual trace for native edits         | Host-level hard blocking/model override, `provider_enforced` (future-only disabled) | Workspace instructions + task wrappers + manual trace |
| **opencode**    | `opencode.json` + agent profile | MCP stdio | `suggest`    | `wrapper_enforced` where wrapper is used, otherwise `advisory` | `mcp_live` + imported/manual trace                      | Cross-host hook parity and provider-owned execution disabled                        | MCP-first with agent profile + manual trace           |
| **Gemini CLI**  | settings + MCP                  | MCP stdio | `suggest`    | `advisory` or `wrapper_enforced` when launched through wrapper | `mcp_live` + imported/manual trace                      | Hard host enforcement beyond wrapper, `provider_enforced` (future-only disabled)    | Config-native MCP workflow + explicit trace record    |

## What Atelier Owns

- Procedural reasoning reuse (ReasonBlocks)
- Runtime loop monitoring
- Session budget optimization guardrails
- Rubric gate verification
- Failure rescue suggestions
- Failure clustering
- Benchmark reporting and cost ledger

## What Hosts Own

- Model choice unless a host-specific wrapper or future provider execution adapter enforces it
- Tool execution and approval
- User interaction
- Memory and fact persistence (see memory integrations)

For the detailed enforcement and trace-confidence contract, see
[`docs/hosts/host-capability-matrix.md`](../hosts/host-capability-matrix.md).

## Installing

```bash
make install   # installs all available hosts
make verify    # verifies code, runtime, and host integrations

# Advanced per-host script usage
bash scripts/install_claude.sh --dry-run
bash scripts/install_codex.sh --print-only
```

All installers are idempotent, back up before writing, and skip gracefully if the CLI
is not on PATH. They support `--dry-run`, `--print-only`, and `--strict`.
