# Requirements: Atelier

**Defined:** 2026-05-23
**Core Value:** Atelier should deliver high-recall engineering context with strict token discipline and low latency.

## v1 Requirements

### Output Policy

- [ ] **OUT-01**: Every public search/code/context renderer applies a shared output policy profile instead of ad hoc field emission
- [ ] **OUT-02**: Every public renderer enforces hard truncation with operation-specific max char caps
- [ ] **OUT-03**: Default response mode is compact (metadata-first) unless explicit flags request richer payloads

### Search and Relation Rendering

- [ ] **SRCH-01**: Search-facing operations return compact symbol pointers by default (name, kind, location, signature when available)
- [ ] **SRCH-02**: `op-callers`, `op-usages`, and `op-callees` share compact relation rendering with bounded related symbol counts
- [ ] **SRCH-03**: Search/relation outputs dedupe repeated hits before rendering

### Context Rendering

- [ ] **CTX-01**: Context output caps entry points, related symbols, and code blocks with deterministic limits
- [ ] **CTX-02**: Context hides import/export noise by default and caps symbols per file
- [ ] **CTX-03**: Context includes at most bounded, truncated code blocks in compact mode

### Outline and Node Behavior

- [ ] **OUTL-01**: `op-outline` performs exact match, case-insensitive fallback, and file/module fallback to recover expected symbols
- [ ] **OUTL-02**: `op-outline` returns member outlines (names, kinds, lines, signatures) without full source bodies by default
- [ ] **OUTL-03**: Container symbols (class/module/interface/etc.) render structural outlines by default unless explicit code inclusion is requested

### Benchmark and Regression Gates

- [ ] **BMRK-01**: Benchmark scoring uses effective tokens (`tokens / max(recall, 0.1)`) as the token efficiency basis
- [ ] **BMRK-02**: CI regression checks fail when recall regresses versus current Atelier baseline
- [ ] **BMRK-03**: CI regression checks fail when effective-token caps are exceeded for benchmarked operations
- [ ] **BMRK-04**: CI regression checks fail when search/context latency exceeds benchmark thresholds

## v2 Requirements

### Future Enhancements

- **FUTR-01**: Add richer verbosity tiers and operation-specific explainability payloads once compact defaults are stable
- **FUTR-02**: Revisit advanced index/watcher behavior after token-discipline milestone goals are met

## Out of Scope

| Feature | Reason |
|---------|--------|
| Full retrieval-engine rewrite | Not required to solve current token inefficiency issue |
| Disabling retrieval depth to force token drops | Risks recall regression and violates milestone goal |
| New unrelated MCP surfaces | Keep milestone focused on output contract quality |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| OUT-01 | Phase 1 | Pending |
| OUT-02 | Phase 1 | Pending |
| OUT-03 | Phase 1 | Pending |
| SRCH-01 | Phase 2 | Pending |
| SRCH-02 | Phase 2 | Pending |
| SRCH-03 | Phase 2 | Pending |
| CTX-01 | Phase 3 | Pending |
| CTX-02 | Phase 3 | Pending |
| CTX-03 | Phase 3 | Pending |
| OUTL-01 | Phase 4 | Pending |
| OUTL-02 | Phase 4 | Pending |
| OUTL-03 | Phase 4 | Pending |
| BMRK-01 | Phase 5 | Pending |
| BMRK-02 | Phase 5 | Pending |
| BMRK-03 | Phase 5 | Pending |
| BMRK-04 | Phase 5 | Pending |

**Coverage:**
- v1 requirements: 16 total
- Mapped to phases: 16
- Unmapped: 0

---
*Requirements defined: 2026-05-23*
*Last updated: 2026-05-23 after milestone requirement definition*
