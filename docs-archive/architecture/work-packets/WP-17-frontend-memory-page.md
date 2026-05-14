---
id: WP-17
title: New `Memory.tsx` page + run-inspector drawer
phase: E
pillar: 1
owner_agent: atelier:code
depends_on: [WP-07, WP-08]
status: done
---

# WP-17 — Frontend: Memory page

## Why

Pillar 1 needs a UI surface or it's invisible. We add a Memory page (core blocks + archival) and a
global Run Inspector drawer that shows, for any run, which blocks were active and which passages
were recalled.

## Files touched

- `frontend/src/pages/Memory.tsx` — new
- `frontend/src/pages/AGENT_README.md` — edit (register the page)
- `frontend/src/App.tsx` — edit (add route)
- `frontend/src/components/RunInspectorDrawer.tsx` — new
- `frontend/src/components/MemoryBlockCard.tsx` — new
- `frontend/src/components/ArchivalSearchBox.tsx` — new
- `frontend/src/services/stub/...` — generated from OpenAPI (no manual edits)
- `frontend/src/pages/Traces.tsx` — edit (add a "Open run inspector" button)

## How to execute

1. Regenerate the frontend SDK first so the new memory endpoints from WP-07/08 are available.

2. `Memory.tsx`:
   - Two-column layout (no boxes — flat per CLAUDE.md design strategy)
   - Left: list of pinned + recent core memory blocks for the active agent_id (filter dropdown
     listing every distinct agent_id from the API)
   - Right: archival search box + result list
   - Inline edit of a memory block opens a diff modal that POSTs `memory_upsert_block` with the
     correct optimistic-lock version. Show conflict UI on 409.

3. `RunInspectorDrawer.tsx`:
   - Slides in from the right when a trace row is clicked
   - Shows: pinned blocks, recalled passages, summarized events count, tokens_pre / tokens_post
   - Links each recalled passage back to its source

4. Use only existing design-system primitives. No new UI library.

5. Every interactive element must have an accessible label and pass `npm run typecheck` strict.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier/frontend
npm run typecheck -- --noEmit
npm test -- --watchAll=false src/pages/Memory.test.tsx
npm test -- --watchAll=false src/components/RunInspectorDrawer.test.tsx
```

Tests must cover:

- Memory page renders without crashing on empty data
- Editing a block sends the correct version → mock 409 shows the conflict UI
- Run inspector renders all sections from a fixture run

## Definition of done

- [x] Memory page reachable at `/memory`
- [x] Run inspector drawer reachable from `/traces`
- [x] All Vitest unit tests pass
- [x] `frontend/AGENT_README.md` updated to register the page
- [x] `INDEX.md` updated; trace recorded
