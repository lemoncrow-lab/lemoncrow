---
id: WP-09
title: In-process sleeptime summarizer (with optional Letta delegate)
phase: B
pillar: 1, 3
owner_agent: atelier:code
depends_on: [WP-06]
status: done
---

# WP-09 — Sleeptime summarizer

## Why

The existing `ContextCompressionCapability` (in `core/capabilities/context_compression/capability.py`)
does TF-IDF + budget pruning of the run ledger. We extend it with a **sleeptime pass** that:

1. paraphrases evicted ledger events into 1-3-sentence chunks (Letta's pattern), and
2. archives them into long-term memory so they can be recalled by `memory_recall` later.

When the Letta sidecar is configured, we delegate to it; otherwise we use a deterministic local
summarizer (template-based, no LLM call) so CI stays hermetic.

## Files touched

- `src/atelier/core/capabilities/context_compression/capability.py` — edit: add `compress_with_sleeptime()`
- `src/atelier/core/capabilities/context_compression/sleeptime.py` — new
- `src/atelier/infra/memory_bridges/letta_adapter.py` — edit: add `summarize_run(...)`
- `src/atelier/gateway/adapters/mcp_server.py` — edit: register `memory`
- `tests/core/test_local_sleeptime.py`
- `tests/infra/test_sleeptime_writes_archival.py`

## How to execute

1. Read the existing `compress_with_provenance()` method end-to-end (it's only ~120 lines). Build
   `compress_with_sleeptime()` as a strict superset:
   - Run the existing dedup + score + budget steps.
   - For every event in `dropped` (i.e. evicted), call the sleeptime summarizer to produce a
     `MemoryChunk` (text + start_event_id + end_event_id + a 1-3-sentence paraphrase).
   - Insert each chunk as an `ArchivalPassage` (source=`block_evict`, source_ref=`run:<session_id>`).
   - Write a `RunMemoryFrame` row capturing tokens_pre/post and the chosen `compaction_strategy`.

2. Local summarizer (`sleeptime.py`):
   - Group consecutive evicted events of the same `kind`.
   - For each group emit a single sentence in the form
     `"[<n> <kind>s] " + last_event.summary[:200]`.
   - Output structure:
     ```python
     class SleeptimeChunk(BaseModel):
         start_event_index: int
         end_event_index: int
         paraphrase: str
     ```

3. Letta delegate:
   - If `LettaAdapter.is_available()`, call `letta_client.summarize_run(...)` (real method on the
     sidecar). Map the response to `SleeptimeChunk`s.
   - On any exception fall back to local summarizer and log WARNING.

4. MCP tool `memory`:
   - Input: `session_id`
   - Output: `&#123; tokens_pre, tokens_post, summary_md, evicted_event_ids, archived_passage_ids, strategy &#125;`

5. Integration test:
   - Build a 200-event ledger with redundant tool outputs.
   - Call `compress_with_sleeptime(token_budget=4000)`.
   - Assert: `tokens_post < tokens_pre * 0.6`; ≥ 1 `ArchivalPassage` row written;
     `RunMemoryFrame` row exists; `memory_recall("the redundant lookup")` returns at least one
     archived chunk.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_local_sleeptime.py \
                     tests/infra/test_sleeptime_writes_archival.py -v

# MCP smoke
LOCAL=1 uv run atelier memory summarize --run-id <some_session_id>  # uses 'memory summarize' alias

make verify
```

## Definition of done

- [ ] `compress_with_sleeptime()` implemented; original `compress_with_provenance()` unchanged
- [ ] Local summarizer is deterministic (same input → same chunks)
- [ ] Letta delegate guarded behind `LettaAdapter.is_available()`
- [ ] `RunMemoryFrame` written on every call
- [ ] Integration test passes
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
