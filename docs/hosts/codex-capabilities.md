# Codex CLI capability assumptions (pinned)

> Findings dated **2026-06-14**. Baseline: OpenAI Codex CLI ~**0.125.x** (April
> 2026 hooks GA), recommended model `gpt-5.5`. Codex moves fast — re-verify the
> `[verify]` rows against the exact version you ship against. The LemonCrow
> integration is written to these documented schemas and **fails open**: if a
> schema assumption is wrong on a given version, the hook degrades to a silent
> no-op instead of blocking the agent.

This is the source of truth for *why* the Codex integration is shaped the way it
is. It records what we rely on from the Codex platform so the implementation is
not re-derived from memory each time.

## Lifecycle hooks (the basis for telemetry + proof-gating)

Codex loads hooks from `hooks.json` or an inline `[hooks]` table in
`config.toml`, using Claude-Code-compatible event names and payloads (it even
ships `CLAUDE_PLUGIN_ROOT` aliases). LemonCrow registers them via the plugin
bundle's `hooks/hooks.json`, with commands resolved through `${PLUGIN_ROOT}`.

| Event | LemonCrow hook | Can block? | Output schema we emit |
| --- | --- | --- | --- |
| `SessionStart` | `update_notification.py` | n/a | `systemMessage` (update notice) |
| `UserPromptSubmit` | `user_prompt.py` | n/a | `systemMessage` (ctx nudge, UI only) |
| `PreToolUse` | `pre_tool_use.py` | **yes** | `hookSpecificOutput.permissionDecision = "deny"` + `permissionDecisionReason` |
| `PostToolUse` | `savings_reporter.py` | feedback | `systemMessage` (rescue nudge only) |
| `PreCompact` / `PostCompact` | `compact.py` | n/a | silent (state only) |
| `Stop` | `stop.py` | n/a | `systemMessage` (session summary) |

- **Block a tool**: `PreToolUse` returns `permissionDecision: "deny"` (or exits
  `2`). LemonCrow uses the JSON form. `[verify]` deny-enforcement has had per-handler
  bugs (openai/codex#20204) — treat deny as best-effort and confirm on your version.
- **Surfaced guidance** uses `systemMessage`, which the existing Codex Stop /
  UserPromptSubmit hooks already prove works on this codebase.

## Tool-coverage ceiling (honest limit)

As of 0.125.0 only the **`shell`**, **`unified_exec`**, **`apply_patch`**, and
**`mcp`** tool handlers emit `PreToolUse`/`PostToolUse` events (openai/codex#20204).
Tools that do **not** fire hooks today: `web_search`, `plan`/`update_plan`,
`list_dir`, `view_image`, and the `multi_agents` family. Consequences for LemonCrow:

- Edit (`apply_patch` / `mcp__lemon__edit`), shell, and MCP-tool telemetry and
  proof-gating **work**.
- Telemetry is **blind** to web-search/plan/multi-agent tool calls. The
  `codex exec --json` collector (headless runs) backfills part of this gap.
- `_normalize_codex_tool()` maps both native (`apply_patch`, `shell`) and
  MCP-prefixed (`mcp__…__edit`) tool names onto the `edit`/`bash` lanes.

## Tool input/response payload shapes we read

Fail-open extraction (any shape we don't recognize is skipped):

- **Edit targets**: `tool_input.edits[].file_path|path|filename`, else
  `tool_input.file_path|path|filename`.
- **Edit diff**: unified diff from `old_string`/`new_string` when present
  (LemonCrow edit), else `git diff HEAD -- <path>` in the workspace.
- **Command**: `tool_input.command` (string or argv list).
- **Return code**: first present of `exit_code|exitCode|returnCode|return_code|code`.
- **Output**: `tool_response.stdout|output` and `tool_response.stderr`.

## Session correlation

The run ledger lives at `runs/<id>.json`. Hooks resolve the id from
`session_state.json` (`active_session_id` → `session_id`) then the payload
`session_id`, and only append when that run file already exists — a hook never
fabricates a ledger. `session_state.json` is keyed by a sha256 hash of the
workspace root (payload `cwd` → `CODEX_WORKSPACE_ROOT` → cwd), matching
`update_notification.py`.

## `[verify]` — confirm against the pinned Codex version before relying on these

- Command-backed `tui.status_line` schema (Phase 2 statusline) — recent feature,
  exact key names not fully enumerated in reachable docs.
- `PermissionRequest` `behavior: "allow"|"deny"` payload (Phase 3).
- Whether Codex exposes a custom **subagent-definition** mechanism (Phase 4) or
  only AGENTS.md + skills.
- Per-handler `deny` enforcement reliability (openai/codex#20204).
- Whether `additionalContext` vs `systemMessage` is preferred for PostToolUse
  feedback on your version (we emit `systemMessage`).

## Primary sources

- Codex hooks: https://developers.openai.com/codex/hooks
- Hook dispatch impl: https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs
- Hook coverage gap: https://github.com/openai/codex/issues/20204
- Config reference: https://developers.openai.com/codex/config-reference
- MCP: https://developers.openai.com/codex/mcp
- AGENTS.md: https://developers.openai.com/codex/guides/agents-md
- Non-interactive `codex exec --json`: https://developers.openai.com/codex/noninteractive
