---
id: WP-V3.1-D
title: Mem0-style four-op memory arbitrator (`memory_upsert_block` write-time arbitration)
phase: V3.1
boundary: Atelier-core
owner_agent: atelier:code
depends_on: [WP-33, WP-34, WP-35, WP-36, WP-39, WP-47, WP-49, WP-50]
supersedes: []
status: done
---

# WP-V3.1-D — Four-op memory arbitrator

## Why

V2 memory writes are append-only: every `memory_upsert_block(label, value, …)` either
inserts a new block or updates the one matching by id. Contradictory facts accumulate
silently. Outdated entries linger. The store gradually becomes noisy and a `memory_recall`
returns inconsistent answers.

Mem0's four-op operator (Apache-2.0, paper arXiv:2504.19413) solves this at write time. On
each new fact:

1. Retrieve top-k similar existing memories via embedding.
2. Single LLM call returns one of `&#123;ADD, UPDATE, DELETE, NOOP&#125;`:
   - **ADD:** new fact is genuinely new; insert.
   - **UPDATE:** new fact refines an existing one; replace its value, preserve id.
   - **DELETE:** new fact contradicts an existing one that's now stale; tombstone the
     existing block and record the contradiction.
   - **NOOP:** new fact is already present; do nothing, bump `last_seen_at`.

V3.1-D wraps `memory_upsert_block` with this arbitrator. Default-on when `[smart]` extra is
installed; passes through to V2 behavior otherwise.

## Files touched

- **NEW:** `src/atelier/core/capabilities/memory_arbitration/__init__.py`
- **NEW:** `src/atelier/core/capabilities/memory_arbitration/arbiter.py`:
  - `arbitrate(new_fact: MemoryBlockInput, store: MemoryStore, embedder: Embedder, *,
k: int = 5) -> ArbitrationDecision`
  - Pipeline:
    1. Embed `new_fact.value`.
    2. `top_k = store.search_passages(embedding, k=5)` (or block-level search if available).
    3. Compose Ollama prompt with structured JSON schema response:
       ```json
       &#123;
         "op": "ADD|UPDATE|DELETE|NOOP",
         "target_block_id": "<id or null>",
         "merged_value": "<string or null>",
         "reason": "<one-line explanation>"
       &#125;
       ```
    4. Call `internal_llm.ollama_client.chat(...,  json_schema=...)` (added in WP-36).
    5. Validate response; clamp invalid ops to `ADD` with a WARNING log.
  - `ArbitrationDecision` Pydantic model carries op + target_block_id + merged_value +
    reason.
- **EDIT:** `src/atelier/gateway/mcp_server.py` — `memory_upsert_block` MCP tool wraps the
  call:
  - If `[smart]` extra is installed AND Ollama is reachable: arbitrate, then apply the op.
  - Otherwise: V2 behavior (direct upsert).
  - Either way, result includes `arbitration: &#123;"op": ..., "reason": ...&#125;` so the host can
    see what happened.
- **EDIT:** `src/atelier/infra/storage/sqlite_memory_store.py` — support tombstone for
  `DELETE` (don't physically remove; mark `deprecated_at`, `deprecated_by_block_id`,
  `deprecation_reason`). Existing `get_block` excludes tombstoned rows by default; new
  optional `include_tombstoned=True` for audit.
- **EDIT:** `src/atelier/infra/memory_bridges/letta_adapter.py` — implement tombstone
  semantics on the Letta side (Letta's metadata field carries the deprecation flag).
- **NEW:** Telemetry: per-op counter
  `memory_arbitration_total&#123;op="ADD|UPDATE|DELETE|NOOP"&#125;`. Surfaces drift in op
  distribution; healthy distribution is ~70% ADD, ~15% UPDATE, ~10% NOOP, \<5% DELETE.

### Tests

- **NEW:** `tests/core/test_arbiter_add.py` — new fact, no similar existing → ADD.
- **NEW:** `tests/core/test_arbiter_update.py` — refinement of existing → UPDATE.
- **NEW:** `tests/core/test_arbiter_delete.py` — contradiction of existing → DELETE +
  tombstone.
- **NEW:** `tests/core/test_arbiter_noop.py` — duplicate of existing → NOOP.
- **NEW:** `tests/core/test_arbiter_invalid_response.py` — Ollama returns malformed JSON;
  fallback is ADD with WARNING log.
- **NEW:** `tests/core/test_arbiter_ollama_unavailable.py` — `[smart]` not installed or
  Ollama down: passes through to V2 behavior.
- **NEW:** `tests/gateway/test_memory_upsert_with_arbitration.py` — MCP smoke; result carries
  `arbitration` payload.
- **NEW:** `tests/infra/test_tombstone_semantics.py` — DELETE doesn't physically remove;
  recall excludes tombstoned by default.

## How to execute

1. **Schema first.** Add `deprecated_at`, `deprecated_by_block_id`, `deprecation_reason`
   columns to `memory_block`. Migrate.

2. **Build arbiter as a pure function** that takes `new_fact + top_k + ollama_response`
   and returns the decision. Easy to test in isolation.

3. **Wire into `memory_upsert_block` with feature gate.** When `[smart]` extra is absent
   or `OllamaUnavailable`, behave exactly like V2.

4. **Test the four ops with golden fixtures.** Each test pre-populates the store with a
   handful of memories, calls the MCP tool with a new fact, mocks Ollama's response to
   each of the four ops, asserts the right state change.

5. **Telemetry from day one.** The op-distribution metric is how we'll know if the
   arbitrator is doing the right thing in production.

## Boundary check

The Ollama call is on the host's _write_ path (synchronous from the host's perspective).
That is **not** the user's hot reasoning path — it adds 300-500ms to a memory write, which
is a rare operation. The host CLI is unaware of the Ollama call; from the host's view,
`memory_upsert_block` is just slightly slower and returns richer info.

The Ollama call is **only** reached via `memory_upsert_block`. No general-purpose Ollama
exposure. Tombstones are reversible (un-deprecate is a future packet if needed).

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv pip install -e ".[smart]"

LOCAL=1 uv run pytest tests/core/test_arbiter_add.py \
                     tests/core/test_arbiter_update.py \
                     tests/core/test_arbiter_delete.py \
                     tests/core/test_arbiter_noop.py \
                     tests/core/test_arbiter_invalid_response.py \
                     tests/core/test_arbiter_ollama_unavailable.py \
                     tests/gateway/test_memory_upsert_with_arbitration.py \
                     tests/infra/test_tombstone_semantics.py -v

# Manual smoke (requires local Ollama):
ollama serve &  ollama pull llama3.2:3b
LOCAL=1 uv run python -c "
from atelier.gateway.mcp_server import memory_upsert_block
print(memory_upsert_block(label='deploy.target', value='production'))
print(memory_upsert_block(label='deploy.target', value='production environment'))
# Expect second call to return arbitration.op == 'NOOP' or 'UPDATE'.
"

make verify
```

## Definition of done

- [ ] Arbiter implemented as a pure function with clean four-op decision.
- [ ] `memory_upsert_block` wraps the arbiter; gracefully passes through to V2 when
      `[smart]` extra absent.
- [ ] Tombstone semantics in both SQLite and Letta backends.
- [ ] All four ops tested with golden fixtures.
- [ ] `memory_arbitration_total` Prometheus counter exported.
- [ ] No new general-purpose Ollama surface added (boundary check — Ollama is reached only
      via this arbitrator).
- [ ] `make verify` green.
- [ ] V3 INDEX (V3.1 section) updated. Trace recorded.
