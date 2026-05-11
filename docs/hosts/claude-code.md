# Claude Code Integration

Atelier integrates with Claude Code via:

1. An MCP server (primary — all V1 + V2 tools)
2. Four specialized agents (`atelier:code`, `atelier:explore`, `atelier:review`, `atelier:repair`)
3. Slash commands for status, context, savings, and evals
4. Skills that auto-trigger on task start, plan check, failure, and trace record
5. Lifecycle hooks (optional, disabled by default)

## Quick Setup (Plugin)

The `integrations/claude/plugin/` directory is the canonical Claude Code plugin. Install it from the Atelier repo:

```bash
make install
```

This uses Claude user MCP scope by default. For project-local `.mcp.json`, run
`bash scripts/install_claude.sh --workspace /path/to/workspace`.

Then bootstrap the engine:

```bash
cd atelier && uv sync
uv run atelier init
```

## Manual MCP Setup

If you prefer to wire the MCP server directly without the plugin:

```json
&#123;
  "mcpServers": &#123;
    "atelier": &#123;
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "$&#123;workspaceFolder&#125;/atelier", "atelier-mcp"],
      "env": &#123;
        "ATELIER_WORKSPACE_ROOT": "$&#123;workspaceFolder&#125;",
        "ATELIER_ROOT": "$&#123;env:HOME&#125;/.atelier"
      &#125;
    &#125;
  &#125;
&#125;
```

## Agents

The plugin provides 4 agents selectable via `claude --agent`:

| Agent             | Purpose                                                           |
| ----------------- | ----------------------------------------------------------------- |
| `atelier:code`    | Main coding agent — runs full reasoning loop (default)            |
| `atelier:explore` | Read-only investigator — retrieves context, reads files, no edits |
| `atelier:review`  | Verifier — runs `lint` + `verify` against a patch                 |
| `atelier:repair`  | Repair specialist — loads run ledger, asks for rescue, verifies   |

## Slash Commands

| Command                               | Description                                                  |
| ------------------------------------- | ------------------------------------------------------------ |
| `/atelier:status [session_id]`        | Show current run plan, facts, blockers, alerts               |
| `/atelier:context <domain>`           | Resolve environment rules and required validations           |
| `/atelier:savings`                    | Calls avoided, tokens saved, rubric failures caught          |
| `/atelier:benchmark [--apply]`        | Run eval suite (dry-run by default)                          |
| `/atelier:analyze-failures`           | Cluster repeated failures; propose blocks/rubrics/eval cases |
| `/atelier:evals list\|run\|promote`   | Manage the eval suite                                        |
| `/atelier:settings [off\|shadow\|on]` | Inspect or change smart-tool mode (default: `shadow`)        |

## Skills

Skills auto-trigger based on context:

| Skill                  | Trigger                                                   |
| ---------------------- | --------------------------------------------------------- |
| `atelier-task`         | Start of every coding task — runs the full reasoning loop |
| `atelier-check-plan`   | Explicit plan validation                                  |
| `atelier-rescue`       | Invoked on repeated failures                              |
| `atelier-record-trace` | End-of-task observable summary                            |

## Hooks (Optional)

Hooks are **disabled by default**. Enable them in `integrations/claude/plugin/hooks/hooks.json` only after you're comfortable with the skills/agent flow.

What each hook does:

- **`pre_tool_use.py`** — On Edit/Write to risky paths (`shopify/`, `pdp/`, `catalog/`, etc.), require a recent successful `lint` in session state.
- **`post_tool_use_failure.py`** — On the second identical Bash failure (same command + error signature), tell the agent to call `rescue`.
- **`stop.py`** — On session stop, ensure `trace` was called.
- **`compact.py`** — On PreCompact/PostCompact, manage context preservation manifest (see section below).

Hook state is kept at `$&#123;workspace&#125;/.atelier/session_state.json`. No secrets, no chain-of-thought stored.

To enable hooks, edit `integrations/claude/plugin/hooks/hooks.json`:

```jsonc
// integrations/claude/plugin/hooks/hooks.json
&#123;
  "PreToolUse": [&#123; "matcher": "Edit|Write|MultiEdit", "enabled": true &#125;],
&#125;
```

### Context Preservation on Compact (`PreCompact` / `PostCompact`)

When Claude Code compacts the conversation, Atelier preserves critical runtime state:

**Pre-Compact Lifecycle:**

1. Claude Code triggers the `PreCompact` hook (before compaction).
2. Atelier's `compact` MCP tool calculates:
   - **Context utilisation %** (tokens used / 200K context window)
   - **Should compact** (true if ≥60% utilized)
   - **Top ReasonBlocks** to preserve (max 3)
   - **Pinned memory blocks** for the agent
   - **Recently edited files** (last 5)
3. The hook persists this manifest to `.atelier/runs/<session_id>/compact_manifest.json`.
4. The manifest survives the host's compaction.

**Post-Compact Lifecycle:**

1. Claude Code triggers the `PostCompact` hook (after compaction).
2. The hook reads the persisted manifest.
3. Atelier records which ReasonBlocks and memory blocks were preserved.
4. On next `/atelier:context` or reasoning call, Atelier re-injects preserved blocks into the new session.

**Example Manifest:**

```json
&#123;
  "created_at": "2026-05-03T17:44:00+00:00",
  "session_id": "abc123def456",
  "should_compact": true,
  "utilisation_pct": 68.5,
  "preserve_blocks": ["block_auth_001", "block_db_config_002"],
  "pin_memory": ["mem_api_token_xyz"],
  "open_files": ["src/auth.py", "src/db.py"],
  "suggested_prompt": "Compact this conversation. Context utilisation: 68.5%. Preserve: block_auth_001, block_db_config_002."
&#125;
```

To enable the compact lifecycle:

```jsonc
// integrations/claude/plugin/hooks/hooks.json
&#123;
  "PreCompact": [&#123; "matcher": "manual|auto", "enabled": true &#125;],
  "PostCompact": [&#123; "matcher": "manual|auto", "enabled": true &#125;]
&#125;
```

## MCP Tools Available

**V1 (core):** `reasoning`, `lint`, `rescue`, `verify`, `trace`, `search`

**V2 (extended):** `reasoning`, `trace`, `trace`, `compact`, `reasoning`, `reasoning`, `read`, `search`, `search`, `compact`

## Reasoning Loop (Full)

The `atelier:code` agent and `atelier-task` skill enforce this loop on every task:

1. `reasoning` — inject relevant procedures
2. `lint` — validate plan before editing (exit 2 = abort)
3. Execute task
4. `verify` — verify output meets domain requirements
5. `trace` — record what happened (for future rescue + block extraction)
