---
id: WP-13
title: Native `/compact` lifecycle (advise + post-hook)
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: [WP-09]
status: done
---

# WP-13 — `/compact` lifecycle integration

## Why

Host-native compaction often fires only after useful state is already under pressure. Atelier
inserts itself earlier and ensures the post-compact session gets the right ReasonBlocks and pinned
memory blocks re-injected.

## Implementation boundary

- **Host-native:** conversation compaction, context-window management, and `/compact` execution stay
  owned by the host CLI.
- **Atelier augmentation:** Atelier advises when to compact and persists a deterministic manifest of
  ReasonBlocks, pinned memory, and recently touched files to restore after host compaction.
- **Not in scope:** do not implement a parallel chat compactor or try to replace the host's
  summarization behavior.

## Files touched

- `integrations/claude/plugin/hooks/compact.py` — edit (currently a stub)
- `integrations/claude/plugin/hooks/hooks.json` — edit (register pre-compact + post-compact)
- `src/atelier/core/foundation/monitors.py` — edit (add `CompactAdvised` event type)
- `src/atelier/gateway/adapters/mcp_server.py` — edit (register `compact`)
- `src/atelier/core/capabilities/context_compression/capability.py` — edit
- `tests/core/test_compact_advise.py`
- `tests/gateway/test_compact_hook_round_trip.py`
- `docs/hosts/claude-code.md` — edit (document the lifecycle)

## How to execute

1. Add `compact(session_id)` MCP tool. Output:

   ```json
   &#123;
     "should_compact": true,
     "utilisation_pct": 62.4,
     "preserve_blocks": ["block_id1", "block_id2"],
     "pin_memory": ["mem_id1"],
     "open_files": ["src/foo.py", "src/bar.ts"],
     "suggested_prompt": "Compact this conversation. Preserve: ..."
   &#125;
   ```

   Logic:
   - `should_compact = utilisation_pct >= 60`
   - `preserve_blocks` = top-3 ReasonBlocks active in the run
   - `pin_memory` = MemoryBlocks with `pinned=True` for the run's `agent_id`
   - `open_files` = last 5 files modified in the run ledger

2. Implement the pre-compact hook (`compact.py`):
   - On every Claude `/compact` event, query `compact` and emit a chat message with
     the suggested preservation list.
   - Persist the manifest to `.atelier/run/<session_id>/compact_manifest.json`.

3. Implement the post-compact hook (same file, different handler):
   - Read the manifest.
   - Call `reasoning(task=<saved task>, agent_id=<run.agent_id>, recall=true)`
     and inject the result as a system note in the new conversation.
   - Re-load pinned memory blocks the same way.
   - Never replace the host's compacted conversation text; only add Atelier's observable runtime
     facts back into the new session.

4. Test:
   - Unit: compact_advise returns sane defaults at 50 %, 65 %, 90 %.
   - Integration: simulate a compact event; assert manifest written; simulate post-compact; assert
     ReasonBlocks + memory re-injected into the session log.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_compact_advise.py \
                     tests/gateway/test_compact_hook_round_trip.py -v
make verify
```

## Definition of done

- [ ] Pre- and post-compact hooks land in `compact.py` (no longer a stub)
- [ ] `compact` registered and reachable
- [ ] Manifest written to disk, restored after compact
- [ ] Tests prove Atelier restores facts around host compaction rather than replacing compaction
- [ ] Claude Code host docs updated
- [x] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
