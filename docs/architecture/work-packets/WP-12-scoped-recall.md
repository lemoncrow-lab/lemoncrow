---
id: WP-12
title: Wire `memory_recall` into automatic context injection
phase: C
pillar: 1, 3
owner_agent: atelier:code
depends_on: [WP-08]
status: done
---

# WP-12 — Scoped recall in context injection

## Why

Pillar 3 needs the agent to _recall_ relevant prior context instead of having stale context
re-pasted. We extend the `reasoning` tool (already exists) to _additionally_
return up to N archival passages relevant to the task — but with strict tags scoping so we never
leak across agents.

## Files touched

- `src/atelier/core/foundation/retriever.py` — edit
- `src/atelier/gateway/adapters/mcp_server.py` — edit (extend tool output)
- `src/atelier/sdk/__init__.py` — edit
- `tests/infra/test_get_reasoning_context_includes_memory.py`
- `docs/engineering/mcp.md` — edit

## How to execute

1. Extend `reasoning` to accept an optional `agent_id` and an optional
   `recall=true|false` (default `true`).

2. When `recall=true` and `agent_id` is provided, after gathering ReasonBlocks call
   `ArchivalRecallCapability.recall(agent_id, query=<task>, top_k=3)` and append the results to
   the rendered context under a clearly-labelled `<memory>` section.

3. Strict scoping: passages must satisfy at least one of:
   - `agent_id == requested.agent_id`, **or**
   - explicit tag `agent:any` (used for global lessons)

   Never return passages from another agent_id.

4. Output additions:
   - `recalled_passages: [&#123;id, source, score&#125;]`
   - `tokens_breakdown: &#123; reasonblocks, memory, total &#125;`

5. Tests:
   - With seeded passages tagged for `atelier:code`, calling with that agent_id surfaces them
   - With seeded passages tagged for `legacy.external-agent`, calling with `atelier:code` returns none
   - Recall can be disabled via `recall=false`

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/infra/test_get_reasoning_context_includes_memory.py -v
make verify
```

## Definition of done

- [x] Tool extended; backward-compat preserved when `recall=false`
- [x] Strict agent_id scoping enforced and tested
- [x] tokens_breakdown surfaced
- [x] Docs updated
- [x] `make verify` green
- [x] `INDEX.md` updated; trace recorded
