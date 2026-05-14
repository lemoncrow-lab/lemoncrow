# context_reuse

## Purpose

Retrieves and ranks reusable reasoning blocks (procedures, dead-ends) for the current task context using hybrid ranking with adaptive online priors, graph propagation, and ANN reranking.

## Entry Point

`__init__.py` — re-exports `ContextReuseCapability` (the only public surface).

## Module Layout

| File            | Responsibility                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------------ |
| `capability.py` | `ContextReuseCapability(store, root)` — orchestrator with RRF + adaptive + graph + ANN ranking |
| `bm25.py`       | BM25 term-frequency ranking with camelCase / stopword handling                                   |
| `ranking.py`    | `rank_blocks()` — hybrid BM25 + recency + success score combiner                                 |
| `dead_ends.py`  | `DeadEndTracker` — in-memory dead-end approach registry                                          |
| `models.py`     | `ReuseSavings`, `RankedProcedure`, `ProcedureCluster` dataclasses                                |

## Key Contracts

- Constructor: `ContextReuseCapability(store: ContextStore, root: Path)`
- `retrieve(*, task, domain, files, tools, errors, limit)` → `list[ScoredBlock]`
- `inject_runtime_reasoning(*, task, domain, files, tools, errors, max_blocks)` → `dict` with `procedures`, `dead_ends`, `rescue_strategies`, `rescue_chains`, `required_validations`, `savings` keys
- `savings_estimate()` → `dict[str, int]`

## Ranking Layers

- RRF baseline fusion: BM25 + FTS + base retriever
- Adaptive prior layer: online per-domain prior (River when available, pure-Python fallback)
- Graph propagation layer: NetworkX PageRank over block relatedness edges
- ANN reranker: HNSW nearest-neighbor boost (hash-vector fallback)

## Where to look next

- `atelier/src/atelier/core/foundation/retriever.py` — `ScoredBlock`, `TaskContext`
- `atelier/src/atelier/core/runtime/engine.py` — how capability is wired
