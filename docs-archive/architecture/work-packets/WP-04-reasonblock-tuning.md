---
id: WP-04
title: Tune ReasonBlock retrieval (dedup + budget) for ≥30% reduction in injected tokens
phase: A
pillar: 2, 3
owner_agent: atelier:code
depends_on: []
status: done
---

# WP-04 — ReasonBlock retrieval tuning

## Why
The existing retriever in `src/atelier/core/foundation/retriever.py` returns up to `max_blocks=5`
full ReasonBlocks. In real workloads two of the five are often near-duplicates (same dead-end,
slightly different procedure). Cutting that cuts the prompt without hurting recall@1.

## Files touched

- `src/atelier/core/foundation/retriever.py` — edit
- `src/atelier/core/capabilities/reasoning_reuse/ranking.py` — edit
- `src/atelier/core/capabilities/reasoning_reuse/capability.py` — edit
- `tests/core/test_retriever_dedup.py` — new
- `tests/infra/test_reasonblock_token_budget.py` — new

## How to execute

1. Read `retriever.py` and `ranking.py` end-to-end (they total ~360 lines).
2. Add a per-call **token budget** parameter (default `2000`) to the retriever's main entrypoint.
   Greedy-pack the highest-scoring blocks until the budget is reached. Use `tiktoken` (already a
   dep) with the `cl100k_base` encoding as a model-agnostic counter.
3. Add a **near-dup filter**:
   - Compute MinHash (LSH) signature over each block's `dead_ends + procedure` joined.
   - For any pair with Jaccard ≥ 0.75, keep the higher-ranked one only.
   - Use `datasketch` (already a dep — `pyproject.toml:18`).
4. Update the ReasonBlock renderer to **drop empty optional fields** in the rendered markdown so
   blocks shrink for free. Match the format the existing `renderer.py` produces.
5. Update the capability's `get_reasoning_context` API to optionally return
   `tokens_used` and `tokens_saved_vs_naive` for telemetry (consumed by WP-14).

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

# Unit: dedup collapses the seeded pair
LOCAL=1 uv run pytest tests/core/test_retriever_dedup.py -v

# Integration: on the seed corpus, the retriever's median tokens drop ≥ 30 %
LOCAL=1 uv run pytest tests/infra/test_reasonblock_token_budget.py -v

# No regression on the existing suite
LOCAL=1 uv run pytest tests/ -k retriever -v
make verify
```

The integration test should encode the assertion explicitly:

```python
# tests/infra/test_reasonblock_token_budget.py
def test_dedup_and_budget_cut_tokens_at_least_30pct(seeded_store):
    naive = list(seeded_store.retrieve(task=TASK, max_blocks=10, dedup=False, token_budget=None))
    tuned = list(seeded_store.retrieve(task=TASK, max_blocks=10, dedup=True,  token_budget=2000))
    naive_tok = sum(count_tokens(render(b)) for b in naive)
    tuned_tok = sum(count_tokens(render(b)) for b in tuned)
    assert tuned_tok <= naive_tok * 0.7, f"only &#123;(1 - tuned_tok/naive_tok)*100:.1f&#125;% reduction"
    # Recall is not regressed: top-1 block id is unchanged
    assert naive[0].id == tuned[0].id
```

## Definition of done
- [ ] Retriever supports `token_budget` and `dedup`
- [ ] Near-dup filter integrated and unit-tested
- [ ] Integration test asserts ≥ 30 % reduction with same top-1 ID
- [ ] Telemetry fields exposed for WP-14
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
