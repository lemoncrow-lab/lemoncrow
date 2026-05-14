---
id: WP-07
title: MCP tools `memory_upsert_block`, `memory_get_block`
phase: B
pillar: 1
owner_agent: atelier:code
depends_on: [WP-06]
status: done
---

# WP-07 — Memory MCP tools

## Why
Surfaces the memory store to agents through the existing MCP gateway, matching the data-model
spec at [§ 8](../IMPLEMENTATION_PLAN_V2_DATA_MODEL.md#8-new-mcp-tools).

## Files touched

- `src/atelier/gateway/adapters/mcp_server.py` — edit: register the two tools
- `src/atelier/gateway/adapters/cli.py` — edit: add `memory upsert-block` and `memory get-block` CLI subcommands
- `src/atelier/sdk/__init__.py` — edit: add typed wrappers
- `tests/gateway/test_mcp_memory_tools.py` — new
- `tests/gateway/test_cli_memory_commands.py` — new
- `docs/engineering/mcp.md` — append the two new tools in the Extended table

## How to execute

1. Inspect the existing tool registration block in `mcp_server.py`. Match the same pattern (Pydantic
   tool input model, dispatch dict, error mapping).

2. Tool: `memory`
   - Input: `agent_id, label, value, [limit_chars, description, read_only, pinned, metadata, expected_version, actor]`
   - Default `actor = "agent:" + agent_id`
   - Calls `make_memory_store(root).upsert_block(...)` inside a try/except that maps
     `MemoryConcurrencyError` → tool error code `409` and `MemorySidecarUnavailable` → `503`
   - Output: `&#123; id, version &#125;`

3. Tool: `memory`
   - Input: `agent_id, label`
   - Output: full `MemoryBlock` JSON (Pydantic `model_dump(mode="json")`)
   - Returns `null` (not an error) on miss

4. Apply the existing redaction filter (`core/foundation/redaction.py`) to `value` and `description`
   on the way in. Reject the call if redaction stripped > 50 % of the input — likely secret
   leakage.

5. CLI mirrors:
   - `atelier memory upsert-block --agent-id ... --label ... --value @file.md [--pinned]`
   - `atelier memory get-block --agent-id ... --label ... [--json]`

6. SDK wrappers in `atelier.sdk.MemoryClient` (already imported as part of `AtelierClient`).

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/gateway/test_mcp_memory_tools.py tests/gateway/test_cli_memory_commands.py -v

# CLI smoke
LOCAL=1 atelier memory upsert-block --agent-id atelier:code --label scratch --value "hello"
LOCAL=1 atelier memory get-block --agent-id atelier:code --label scratch --json | grep -q "hello"

# Redaction guard
echo "AKIAIOSFODNN7EXAMPLE secretvalue" | LOCAL=1 atelier memory upsert-block --agent-id t --label leak --value @/dev/stdin || echo "rejected as expected"

make verify
```

## Definition of done
- [ ] Two MCP tools registered, both reachable via `atelier-mcp` (verify with the existing
      MCP test harness)
- [ ] CLI mirrors work
- [ ] Redaction filter applied
- [ ] Optimistic-lock errors surface as MCP error code `409`
- [ ] `make verify` green
- [ ] `docs/engineering/mcp.md` updated
- [ ] `INDEX.md` updated; trace recorded
