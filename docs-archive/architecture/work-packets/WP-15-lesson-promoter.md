---
id: WP-15
title: LessonPromoter capability + MCP tools `lesson_inbox/decide`
phase: D
pillar: 2
owner_agent: atelier:code
depends_on: [WP-02, WP-05]
status: done
---

# WP-15 — Lesson promoter

## Why

Pillar 2's continuous-learning loop. Today, `core/improvement/failure_analyzer.py` clusters failed
traces. We extend it into an end-to-end pipeline: failed-trace → embed → cluster → draft a
ReasonBlock-or-rubric-edit candidate → surface to a human reviewer.

## Files touched

- `src/atelier/core/capabilities/lesson_promotion/__init__.py` — new
- `src/atelier/core/capabilities/lesson_promotion/capability.py` — new
- `src/atelier/core/capabilities/lesson_promotion/draft.py` — new
- `src/atelier/core/improvement/failure_analyzer.py` — edit (emit lessons)
- `src/atelier/gateway/adapters/mcp_server.py` — edit (register two tools)
- `src/atelier/gateway/adapters/cli.py` — edit (`lesson list`, `lesson approve`, `lesson reject`)
- `src/atelier/sdk/__init__.py` — edit
- `tests/core/test_lesson_drafting.py`
- `tests/infra/test_lesson_promotion_precision.py`
- `tests/fixtures/200_failed_traces.jsonl` — new

## How to execute

1. Pipeline:

   ```
   record_trace ──► (failure_analyzer cluster) ──► LessonPromoter.ingest_trace
                                                       │
                                                       ▼
                                          embed(commands_run + errors_seen + diff_summary)
                                                       │
                                                       ▼
                                  k-NN against last 30 days of LessonCandidate inbox
                                                       │
                                                       ▼
                            ┌──────────────────────────┴───────────────┐
                            │                                          │
                cluster size ≥ 3 (after this trace)         cluster size 1..2
                            │                                          │
                            ▼                                          ▼
                draft candidate ─► persist with status=inbox       no-op (waits)
                            │
                            ▼
                surface in /v1/lessons/inbox API
   ```

2. Drafting heuristics (in `draft.py`):
   - If all clustered traces failed the same rubric check → propose `kind="new_rubric_check"` with
     a synthesized check name.
   - If errors_seen share a common substring of length ≥ 12 → propose `kind="new_block"` with
     `dead_ends=[that substring]` and `procedure=["Investigate <verb> in <component>"]`.
   - Else → propose `kind="edit_block"` against the highest-overlap existing block, suggesting an
     additional dead_end entry.

3. MCP tools:
   - `atelier lesson inbox(domain?, limit?)` → `[LessonCandidate]`
   - `atelier lesson decide(lesson_id, decision, reviewer, reason)` → on approve, calls existing
     `atelier_extract_reasonblock` for `new_block` / mutates the target block for `edit_block` /
     adds the rubric check for `new_rubric_check`. Writes a `LessonPromotion` row.

4. Integration test loads the 200-trace fixture (you'll need to create it from the existing
   benchmarks/swe seed — read `benchmarks/swe/datasets.py` to see available fixtures). Asserts
   **precision ≥ 0.7** against a hand-labelled ground truth file.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_lesson_drafting.py \
                     tests/infra/test_lesson_promotion_precision.py -v

# CLI
LOCAL=1 atelier lesson list --json | head
LOCAL=1 atelier lesson approve <id> --reviewer pankaj --reason "matches our standard"

make verify
```

## Definition of done

- [ ] Pipeline implemented end-to-end
- [ ] Precision ≥ 0.7 on the 200-trace fixture
- [ ] MCP tools + CLI mirrors land
- [ ] Promotion writes a `ReasonBlock` row through the existing `extract_reasonblock` path
- [x] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
