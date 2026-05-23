# Architecture

This directory is the live source of truth for Atelier's repository structure.

Start here when you need to answer:

- which layer owns a concern
- where new code should live
- which file or schema is the source of truth
- what an agent should read before making a cross-cutting change

Read in this order:

1. [layers.md](layers.md)
2. [domain-map.md](domain-map.md)
3. [../design/core-beliefs.md](../design/core-beliefs.md)

## Code Intelligence Stack

The code-intelligence subsystem routes agent queries by shape through a layered stack. See ADR [`docs/decisions/001-symbol-first-mcp.md`](../decisions/001-symbol-first-mcp.md) for the full rationale.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ MCP surface — existing tools, extended with new ops                      │
│                                                                          │
│   code(op="search", …)             ← name-first symbol lookup (exists)  │
│   code(op="usages", …)             ← M3, NEW op                         │
│   code(op="callers"|"callees", …)  ← M8, NEW ops                        │
│   code(op="pattern", …)            ← M5, NEW op (ast-grep)              │
│   code(op="recall", …)             ← M7, NEW op (or under memory)       │
│   code(op="blame", …)              ← M15, NEW op (who/when/churn)       │
│   code(op="search" scope="deleted" since="Nd" touched_by=X)             │
│                                    ← M14, temporal + graveyard filters   │
│   edit(edits=[{kind:"symbol",…}])  ← M4, NEW rich-edit descriptor       │
│   read(...)                        ← already outline-first; no M change  │
│   search(...)                      ← stays as text/regex complement      │
│   memory(op="recall_symbol", …)    ← M7 alternative; choose on claim    │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────────────┐
│ SymbolIntelStore (routing + cache + budget + query-shape detection)      │
│  - name query   → SCIP (microseconds, precomputed)                       │
│  - pattern      → ast-grep (structural, cross-language)                  │
│  - NL query     → embeddings (semantic, hybrid RRF)                      │
│  - text search  → Zoekt (>500k LOC) or ripgrep (small repos)            │
│  - deleted/hist → Git History Index (pygit2)                             │
│  - fallback     → LocalAdapter (CodeContextEngine + tree-sitter)         │
│  Content-addressed cache · token-budget packer · trace always on         │
└──┬──────────┬───────────┬──────────┬────────────┬────────────────────────┘
   │          │           │          │            │
   ▼          ▼           ▼          ▼            ▼
┌──────┐ ┌────────┐ ┌──────────┐ ┌──────┐ ┌─────────────────────────────┐
│ SCIP │ │ast-grep│ │Embeddings│ │Zoekt │ │ Git History (M14, M15)      │
│  M1  │ │   M5   │ │   M6     │ │ M16  │ │  walker.py  (pygit2)        │
│★core │ │★core   │ │(vec+RRF) │ │scale │ │  graveyard.py  (SQLite)     │
└──────┘ └────────┘ └──────────┘ └──────┘ │  blame.py   (churn score)  │
   │                                       └────────────┬────────────────┘
   ▼                                                    │
.scip files                                ┌────────────▼────────────────┐
precomputed once,                          │ Symbol Graveyard DB         │
queried in µs                              │ deleted · renamed · blame   │
(scip-python ·                             │ churn · temporal filters    │
 scip-typescript ·                         └─────────────────────────────┘
 scip-go · scip-rust ·
 scip-java · …)

   Cross-language edges (M17, partial):
┌───────────────────────────────────────┐
│  ctypes/cffi  Python→C                │
│  subprocess   TypeScript/Go→Python    │
│  dynamic_import  Python→Python        │
│  confidence-scored · soft edges ok   │
└───────────────────────────────────────┘

   Optional backend (decided in M18 evaluation):
┌──────────────────────────────────────┐
│  SrcCliAdapter  (if org runs SG)     │
│  or Sourcegraph embedded backend     │
│  replaces Zoekt if memory-feasible   │
└──────────────────────────────────────┘
```
