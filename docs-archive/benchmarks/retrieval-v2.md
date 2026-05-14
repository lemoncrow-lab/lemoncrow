# Retrieval V2

Retriever version `2` is the first measured rebuild of `get_reasoning_context` retrieval against a fixed regression set.

## Regression Set

- Source: `benchmarks/retrieval/ground_truth.jsonl`
- Harness: `tests/core/test_retriever_eval.py`
- Rubric: `atelier.retrieval.recall`
- Reproduce: `ATELIER_RETRIEVAL_EVAL_VERBOSE=1 uv run python -m pytest tests/core/test_retriever_eval.py -q -s`

## Kept Changes

- Replaced ANN hash-vector reranking with `make_embedder()` output and cached per-block vectors in `.atelier/vector_cache.sqlite`.
- Computed `vector_scores` once in `rank_reusable_procedures()` and passed them into the base retriever.
- Removed the base retriever pre-fusion `min_score=0.15` clip by setting the internal call to `0.0`.
- Expanded BM25/query text with full error text plus file basenames and tools.
- Expanded BM25 document text with `dead_ends` and `procedure`.
- Softened the quality multiplier to `rrf * (0.8 + 0.4 * bayesian_success * recency)`.

## Removed Change

- Did not keep the broad FTS query expansion to `task + domain + files + tools + full errors`. `search_blocks()` is an exact-phrase FTS query, and the broader phrase regressed MRR. The surviving change keeps FTS on the single most informative task or error phrase.

## Aggregate Delta

| Metric                            | Baseline |       V2 |     Delta |
| --------------------------------- | -------: | -------: | --------: |
| `query_count`                     |       26 |       26 |         0 |
| `recall_at_5`                     | 0.884615 | 1.000000 | +0.115385 |
| `mrr`                             | 0.830128 | 0.935897 | +0.105769 |
| `ndcg_at_5`                       | 0.843488 | 0.952379 | +0.108892 |
| `mean_distinct_domains_per_query` | 4.038462 | 3.692308 | -0.346154 |

`mean_distinct_domains_per_query` is still reported, but it is observational rather than a blocking gate. Relevance improved materially while domain spread narrowed modestly.

## Per-Query Delta

Only four cases changed relative to the baseline snapshot. The remaining 22 cases were unchanged.

| Case                                          | Recall       | Reciprocal Rank | NDCG@5            |
| --------------------------------------------- | ------------ | --------------- | ----------------- |
| `cross_domain_shopify_identity_dead_end_only` | `0.0 -> 1.0` | `0.0 -> 1.0`    | `0.0 -> 1.0`      |
| `cross_domain_jsonld_procedure_only`          | `0.0 -> 1.0` | `0.0 -> 0.5`    | `0.0 -> 0.630930` |
| `cross_domain_audit_procedure_only`           | `0.0 -> 1.0` | `0.0 -> 0.5`    | `0.0 -> 0.630930` |
| `cross_domain_repeated_loop_procedure_only`   | `1.0 -> 1.0` | `0.25 -> 1.0`   | `0.430677 -> 1.0` |

## Verification Gate

The retrieval regression is now gated by `atelier.retrieval.recall` with these required checks:

- `recall_at_5_improved`
- `mrr_improved`
- `cold_start_block_in_top_5`
- `cross_domain_block_retrievable`

The current retrieval test suite verifies the gate in `tests/core/test_retriever_eval.py`.
