---
id: WP-01
title: Author ADR-001 explaining the V2 architecture
phase: A
pillar: foundation
owner_agent: atelier:code
depends_on: []
status: done
---

# WP-01 — Author ADR-001

## Why
We need a single source of truth that records the V2 architecture decisions and their alternatives,
so future contributors and reviewer agents understand *why* this shape was chosen. The ADR is also
the contract every other packet links back to.

## Files touched
- **Create** `docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md`
- **Append** one row to `docs/internal/engineering/decisions/DEPRECATIONS.md` (only if it does not
  exist, append a "no entries yet" placeholder under the V1.x heading; otherwise leave alone)

> If `docs/internal/engineering/decisions/` does not exist, create it and add `_category_.json`
> with `&#123;"label": "Decisions", "position": 8&#125;`.

## How to execute

1. Read `docs/architecture/IMPLEMENTATION_PLAN_V2.md` end-to-end.
2. Draft the ADR with these sections:
   - **Status:** Accepted (you may set Accepted because the user is the project owner)
   - **Context:** What problem V2 solves; cite the ReasonBlock + Trace gap that motivated it
   - **Decision:** The three pillars; the vendoring posture for Letta; the context-savings plan
   - **Alternatives considered:**
     1. Fork Letta — rejected (forever-divergence risk)
     2. Build memory ground-up — rejected (we'd recreate Letta's mistakes)
     3. Replace ReasonBlocks with Letta blocks — rejected (different semantics: procedure vs fact)
   - **Consequences:** (a) extra optional dependency; (b) two stores in the codebase; (c) need to
     keep the Embedder interface stable; (d) lesson promotion requires a human reviewer
   - **References:** link to the plan + data-model docs; link to the Letta repo

3. Keep it under 300 lines. Use prose, not bullet soup.

## Acceptance tests

```bash
# 1. ADR file exists and is non-empty
test -s docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md

# 2. ADR mentions all three pillars by exact name
grep -q "Stateful memory" docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md
grep -q "ReasonBlocks evolution" docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md
grep -q "Context savings" docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md

# 3. ADR cites Letta as Apache 2.0 and vendored, not forked
grep -qE "Apache.?2.0" docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md
grep -qi "vendor" docs/internal/engineering/decisions/001-v2-stateful-memory-and-context-savings.md
```

## Definition of done
- [ ] ADR written and committed (locally — do not push)
- [ ] Acceptance tests above all pass
- [ ] `INDEX.md` updated: WP-01 status flipped to `done`
- [ ] `trace` called with the WP-01 ID in `output_summary`
