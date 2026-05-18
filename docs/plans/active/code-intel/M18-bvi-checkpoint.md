# M18 — Build-vs-integrate checkpoint

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> **Must run before M16 (Zoekt) is started.** No code ships in this milestone.
> Time-box: 2 working days.

## Goal

Before building M16 (Zoekt backend), formally evaluate whether any existing
off-the-shelf tool already covers the use case well enough to adopt instead.
The ADR (`docs/decisions/001-symbol-first-mcp.md`) rejected Serena but did not
evaluate Sourcegraph, Cody, or emerging `scip-mcp` servers. This milestone
closes that gap with a structured evaluation so the M16 decision is documented
and revisitable.

The question is not "is Sourcegraph better?" but "does adopting it save more
implementation time than the integration overhead costs?" and "does it give us
token-budget-aware responses, or do we still need our own packing layer?"

## What to evaluate

### Candidate 1: Sourcegraph `src` CLI as a search backend

The `src` CLI is Sourcegraph's official search client. It supports:
- Code search (regex, structural with `patterntype:structural`)
- Symbol search
- File search
- Diff/commit search

**Evaluation questions:**
- Does `src search -json` return per-result byte ranges usable with our
  BudgetPacker?
- Is the latency acceptable without a local Sourcegraph instance (cloud API)?
- Can it query a private self-hosted repo without setting up a full Sourcegraph
  instance?

**Decision gate:** if `src` covers 80%+ of M16 use cases (large-repo text
search), build a thin `SrcCliAdapter` instead of running Zoekt locally.

### Candidate 2: Sourcegraph self-hosted (Community Edition)

Sourcegraph CE runs as a Docker Compose stack. It includes:
- Zoekt (built in)
- SCIP indexing (built in)
- Blame + code search
- GraphQL API

**Evaluation questions:**
- Memory footprint at idle: < 500 MB? (Atelier target: additive overhead < 200 MB)
- Can it be started/stopped as an Atelier subprocess (not a persistent daemon)?
- Does the GraphQL API expose `budget_tokens`-style response truncation?

**Decision gate:** if idle memory > 500 MB, it's a non-starter as an embedded
component. If memory is acceptable, build a `SourcegraphAdapter` that wraps
the GraphQL API and routes large-repo queries to it.

### Candidate 3: Emerging `scip-mcp` servers

Check GitHub for projects that expose SCIP query APIs over MCP since the ADR
was written (2026-05-18). Search:
- `github.com/search?q=scip+mcp&type=repositories`
- `github.com/search?q=scip+mcp+server&type=repositories`
- Sourcegraph's own MCP announcement track

**Decision gate:** if a production-grade `scip-mcp` server exists (> 100
stars, recent commits, covers M1–M3 use cases), evaluate replacing our own M1
SCIP adapter with it. This would redirect weeks of M1 effort to integration
work instead.

### Candidate 4: Zoekt standalone (the build path)

If no candidate above passes its gate, proceed with M16 as specified:
- Zoekt binary (Go, ~15 MB)
- `ZoektServer` subprocess manager
- `ZoektClient` HTTP wrapper

This is the safe fallback — Zoekt is mature, maintained by Sourcegraph, and
has a simple HTTP API.

## Quantitative pass/fail rubric

Each candidate is scored against **9 binary criteria** on a fixture
megarepo (the Atelier repo padded with `cpython` + `typescript` source as
sub-trees → ~3M LOC). A candidate passes overall if it scores **≥ 7/9**;
otherwise the build path (Zoekt standalone) is chosen.

| # | Criterion | Pass condition |
|---|---|---|
| 1 | Cold text-search latency | < 1.5s for query `"def authenticate"` across 3M LOC |
| 2 | Warm text-search latency | < 50ms for same query repeated |
| 3 | Regex query support | `(class|interface)\s+Auth\w+` returns ≥ 5 matches in fixture |
| 4 | File-scoped query | `query AND file:src/auth/` reduces results without separate filter pass |
| 5 | Memory at idle | < 500 MB additive overhead vs Atelier alone |
| 6 | Memory under load (50 queries/min) | < 800 MB additive overhead |
| 7 | Per-result byte ranges in response | Response includes `byte_start`/`byte_end` or equivalent for BudgetPacker |
| 8 | Private-repo operation | Works without external network round-trip after init |
| 9 | Integration time | Adapter + tests deliverable within 3 working days |

## Evaluation matrix

Complete this table as part of the evaluation; one ✅/❌ per cell:

| Candidate | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | Score | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `src` CLI adapter         | ? | ? | ? | ? | n/a | n/a | ? | ? | ? | _/9 | ? |
| Sourcegraph self-hosted   | ? | ? | ? | ? | ? | ? | ? | ? | ? | _/9 | ? |
| `scip-mcp` (if exists)    | ? | ? | ? | ? | ? | ? | ? | ? | ? | _/9 | ? |
| Zoekt standalone (default)| ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 9/9 | ✅ baseline |

Zoekt is the baseline because the M16 spec already commits to those numbers
based on Sourcegraph's published benchmarks; the table forces every
alternative to clear the same bar.

## Output

A short (< 1 page) evaluation memo added to this file as an appendix, filling
in the matrix above and stating:

1. Which candidate was selected (or "Zoekt standalone — no better option found").
2. Why.
3. Any risks or follow-up questions.

The memo becomes the living record so this decision can be revisited if the
tool landscape changes (e.g., Sourcegraph releases an embeddable MCP server).

## Exit criteria

- Evaluation matrix completed.
- Decision memo written and appended to this file.
- If "Sourcegraph src CLI" or "scip-mcp" wins: a follow-up PR updating M16
  with the adapter approach.
- If "Zoekt standalone" wins: M16 unblocked.
- No code ships in this milestone.

---

## Evaluation memo (to be filled in on claim)

> **Date:** _____
> **Evaluator:** _____

### Findings

_Fill in after evaluation._

### Decision

**Selected approach:** _____

**Rationale:** _____

**Risks:** _____
