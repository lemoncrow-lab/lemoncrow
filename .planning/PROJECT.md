# Atelier

## What This Is

Atelier is an agent reasoning runtime with MCP tools for code retrieval, search, editing, shell, memory, and workflow orchestration. It is designed to keep agent context efficient while preserving strong recall and execution speed. This milestone focuses on reducing token cost in code/context outputs without weakening retrieval quality.

## Core Value

Atelier should deliver high-recall engineering context with strict token discipline and low latency.

## Current Milestone: v1.0 Atelier Token-Cost Reduction

**Goal:** Achieve CodeGraph-level or better effective token efficiency while maintaining or improving recall and keeping latency fast.

**Target features:**
- Unified output policy with hard truncation and compact defaults
- Compact normalized rendering for search/relation/context outputs
- `op-outline` recall fix to 1.0 with container-outline behavior
- Verbosity flags, benchmark scoring alignment, and regression gates

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Standardize output policy and hard caps across code/search/context surfaces
- [ ] Keep recall at or above current Atelier while lowering effective tokens per benchmark operation
- [ ] Fix `op-outline` matching/children extraction so recall reaches 1.0
- [ ] Add benchmark and CI regression gates for recall, effective tokens, and latency

### Out of Scope

- Retriever rewrite or architecture replacement — output shaping first
- SCIP/watcher/routes expansion work not needed for this milestone objective

## Context

- Current benchmark deltas show strong latency and acceptable recall but excessive output verbosity in multiple search/context rows.
- Existing operations such as `op-impact` and `op-callees` already demonstrate efficient payloads and should be preserved.
- This effort should normalize response contracts, not reduce retrieval depth to force token reductions.

## Constraints

- **Quality:** Recall must stay at or above current Atelier baseline
- **Token budget:** Effective tokens must not exceed CodeGraph for comparable operations where CodeGraph recall is 1.0
- **Latency:** Search/context p95 should remain below 100 ms on benchmark fixture
- **Scope:** Incremental changes only; avoid broad subsystem rewrites

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Output-first optimization | Retrieval quality is already competitive; verbosity is main cost driver | — Pending |
| Compact-by-default responses | Most workflows need pointers/structure, not full code dumps | — Pending |
| Hard char caps in all renderers | Prevent unbounded token blowups from edge-case payloads | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-23 after milestone initialization*
