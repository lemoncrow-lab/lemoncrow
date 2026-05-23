# Roadmap: Atelier Milestone v1.0 — Token-Cost Reduction

**Milestone:** v1.0  
**Milestone Name:** Atelier Token-Cost Reduction  
**Requirements:** 16 mapped / 16 total  
**Coverage:** 100%

## Phases

| # | Phase | Goal | Requirements |
|---|-------|------|--------------|
| 1 | Output Policy Foundation | Introduce shared output-policy primitives and hard truncation used by all renderers | OUT-01, OUT-02, OUT-03 |
| 2 | Search/Relation Compact Rendering | Normalize compact symbol shapes and compact relation renderers across search operations | SRCH-01, SRCH-02, SRCH-03 |
| 3 | Context Compact Mode | Enforce bounded context rendering with deterministic structure and capped code inclusion | CTX-01, CTX-02, CTX-03 |
| 4 | Outline Recall and Container Behavior | Fix `op-outline` recall and make container nodes outline-first by default | OUTL-01, OUTL-02, OUTL-03 |
| 5 | Benchmark Governance and Gates | Align scoring and add regression checks for recall, effective tokens, and latency | BMRK-01, BMRK-02, BMRK-03, BMRK-04 |

## Phase Details

### Phase 1: Output Policy Foundation
**Goal:** Centralize output shaping and hard caps for all relevant surfaces.

**Success criteria:**
1. Shared output policy object(s) exist and are consumed by search/code/context renderers.
2. Hard truncation utility is applied uniformly to public renderer outputs.
3. Compact defaults are enforced when verbosity flags are absent.

### Phase 2: Search/Relation Compact Rendering
**Goal:** Cut token overhead in search/relation operations without reducing internal retrieval quality.

**Success criteria:**
1. Search operations render compact pointer-style result rows by default.
2. Relation operations (`callers/usages/callees`) produce bounded compact lists.
3. Duplicate hits are removed before output generation.

### Phase 3: Context Compact Mode
**Goal:** Keep context useful but bounded through strict output structure and caps.

**Success criteria:**
1. Entry points, related symbols, and code blocks are capped deterministically.
2. Import/export noise is suppressed in default compact mode.
3. Context output respects max-per-file and total-size constraints.

### Phase 4: Outline Recall and Container Behavior
**Goal:** Resolve outline recall failures while preserving compact output discipline.

**Success criteria:**
1. `op-outline` fallback matching resolves expected symbols for benchmark queries.
2. Outline output includes members with lines/signatures and avoids full container body dumps.
3. Container symbols render outline-first unless explicit code inclusion is requested.

### Phase 5: Benchmark Governance and Gates
**Goal:** Enforce measurable outcomes via benchmark scoring and CI protections.

**Success criteria:**
1. Effective-token scoring uses `tokens / max(recall, 0.1)` for reporting and comparisons.
2. Regression tests fail on recall drops or token-cap violations for benchmark operations.
3. Regression tests fail when latency thresholds for search/context are exceeded.

## Notes

- This roadmap prioritizes output contract and rendering behavior first, not retrieval simplification.
- If any phase threatens recall, adjust rendering strategy rather than reducing retrieval depth.

---
*Created: 2026-05-23*
