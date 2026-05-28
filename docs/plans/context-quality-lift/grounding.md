# Grounding — Augment research vs. Atelier today

> Read this before any milestone file. If a milestone disagrees with grounding, grounding wins.

## Research source

Research memo derived from public Augment Code documentation, engineering blog posts, and third-party reviews, May 2026. Key sources:

- [Real-Time Index blog](https://www.augmentcode.com/blog/a-real-time-index-for-your-codebase-secure-personal-scalable) — indexing pipeline, SHA-256 fingerprinting, per-user scope
- [Context Lineage announcement](https://www.augmentcode.com/blog/announcing-context-lineage) — commit-summary technique
- [Coordinator-Implementor-Verifier guide](https://www.augmentcode.com/guides/coordinator-implementor-verifier) — agent loop architecture
- [Augment Prism blog](https://www.augmentcode.com/blog/augment-prism-model-routing-to-reduce-cost-and-maintain-quality) — cache-aware per-turn routing
- [Auggie tops SWE-Bench Pro](https://www.augmentcode.com/blog/auggie-tops-swe-bench-pro) — headline 15–17 problem gap with identical underlying model
- [Context Engine MCP](https://www.augmentcode.com/blog/context-engine-mcp-now-live) — 80% PR-quality improvement on Elasticsearch benchmark (vendor-run; treat magnitude with skepticism)

## Core thesis (load-bearing for the whole plan)

Augment's quality advantage is **not** a better base model. They run the same Claude Opus 4.5 as Claude Code and Cursor. Their advantage is:

1. **History-aware retrieval** (Context Lineage) — commits, not just current files.
2. **Scoped pull-model context** — per-subtask, not per-session.
3. **Cache-aware model routing** (Prism) — refuse to switch models when KV-cache delta > quality delta.
4. **Layered verification with structured feedback** — deterministic checks fed back as counterexamples, not pass/fail.

Everything in this plan attacks one of those four levers.

## Atelier-today map

Scan performed against `src/atelier/core/capabilities/` and `src/atelier/infra/code_intel/` on 2026-05-28.

| Augment lever | Atelier status | Where it lives |
|---|---|---|
| Semantic context engine (SCIP-style + tree-sitter) | ✅ Have | `core/capabilities/code_context/engine.py`, `infra/code_intel/scip/` |
| Repo map / PageRank-weighted retrieval | ✅ Have | `core/capabilities/repo_map/` |
| Content-addressed per-file index (SHA-256) | ✅ Have | `core/capabilities/semantic_file_memory/indexer.py` |
| BM25 + dependency-graph hybrid search | ✅ Have | `core/capabilities/context_reuse/bm25.py`, `semantic_file_memory/search.py` |
| Git history walker | 🟡 Partial | `infra/code_intel/git_history/walker.py` (walks commits; no LLM summary, no embedding) |
| **Context Lineage (semantic commit history)** | ❌ **Missing** | — → **M1** |
| Per-subtask scoped pull context | 🟡 Partial | `core/capabilities/context_reuse/capability.py` scores and ranks but exposes no explicit "subtask" API |
| **Scoped pull-context entry point** | ❌ Missing | — → **M4** |
| 5-tier model routing | ✅ Have | `core/capabilities/model_routing/router.py` (deterministic / local_slm / cheap_llm / frontier_llm / human_review) |
| Prefix-cache planner with static/branch/turn split | ✅ Have | `core/capabilities/prefix_cache/planner.py` |
| **Router ↔ planner integration (cache cost-aware switches)** | ❌ **Missing** | — → **M2** |
| Cost-quality proof gate | 🟡 Partial | `core/capabilities/proof_gate/capability.py` (release-gate only; no per-step loop) |
| Failure analysis primitives | ✅ Have | `core/capabilities/failure_analysis/` |
| **Structured counterexample feedback into agent loop** | ❌ **Missing** | — → **M3** |
| Persistent cross-session memory | ✅ Have | `core/capabilities/archival_recall/`, `cross_vendor_memory/`, MCP `memory` tool |
| Loop / rescue detection | ✅ Have (Atelier ahead) | `core/capabilities/loop_detection/rescue.py` — Augment does not document an equivalent |
| Lesson promotion (learn from sessions) | ✅ Have (Atelier ahead) | `core/capabilities/lesson_promotion/` — closer to Augment Cosmos than Cosmos itself is to the public |
| Quantized ANN over embeddings | ❌ Missing | Not in this plan — only matters >10M LOC |
| Custom code embedding models with hard negatives | ❌ Missing | Not in this plan — months of R&D |
| Full CIV DAG decomposition | ❌ Missing | Not in this plan — defer until SWE-bench is the explicit goal |

## Not in this plan (and why)

- **Quantized ANN vector search.** Augment uses bit-vector neighborhood + precise rerank to keep <200ms search on 100M+ LOC. Atelier's largest realistic repos are <5M LOC where SQLite + tree-sitter already meets latency. Revisit if a user reports >10M LOC.
- **Custom embedding models trained with hard-negative mining.** Real lift, but requires training infrastructure and a labelled retrieval corpus. Hybrid lexical+vector rerank (existing) covers ~70% of the gain at ~5% of the cost.
- **DAG-based Coordinator-Implementor-Verifier with worktree-isolated implementors.** The headline Augment architecture, but the value is parallel sub-agent execution. Until parallel sub-agents are an explicit goal (e.g. SWE-bench leaderboard), the single-agent loop with M1/M3 captures most of the quality gain.
- **Cloud-hosted multi-tenant index.** Augment runs Bigtable + PubSub + per-user namespaces. Atelier is local-first by design (`~/.atelier/`); cloud is a non-goal.
- **Cosmos-style org-wide persistent memory.** Atelier's `lesson_promotion/` + `cross_vendor_memory/` already cover the durable-memory primitive. Org-wide sharing is a future product question, not a quality lever.

## Calibration on the headline numbers

The research memo reports several Augment-published metrics. For planning purposes, calibrate them as follows:

| Number | Source | Treat as |
|---|---|---|
| +15–17 SWE-bench Pro problems vs. Claude Code (same model) | Augment blog, with methodology | Credible; directionally correct. Ceiling for whole plan. |
| 67% on CCEval vs. Copilot 50% | Augment blog | Credible — CCEval is an external benchmark. |
| +80% PR completion quality (Elasticsearch MCP test) | Augment blog, Augment-scored | Marketing. Use only as evidence the direction works. |
| Prism: +2-3% SWE-bench at -7 to -12% cost | Augment blog | Credible; matches the underlying technique (cache-aware routing). |
| "Hard-negative mining" claim | Codacy third-party blog | Unverified. Plausible. Not load-bearing here. |

## What "better quality" means for this plan

We explicitly use the internal eval defined in [`index.md`](index.md) §"Success metric" — 30 multi-file edits from this repo's own history. We do **not** chase public benchmark numbers because:

- SWE-bench Pro requires harness investment that isn't on this plan.
- Public benchmarks are gameable; the agent shipping to Atelier users is the agent we should measure.
- Repo-local eval gives ground truth (the original PR's tests) for free.
