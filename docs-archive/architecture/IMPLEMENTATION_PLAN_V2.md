# Atelier V2 — Implementation Plan

**Status:** Draft v1 · 2026-05-03
**Owner:** Pankaj (root coordinator)
**Audience:** Small subagents (`atelier:code` / `atelier:explore` / `atelier:review`) executing one atomic
work-packet at a time. Read the [work-packets index](work-packets/INDEX.md) for the dispatch graph.

---

## 1. Vision

Today Atelier is a **reasoning runtime** for coding/operational agents: it ships ReasonBlocks, plan
checks, rubric gates, failure rescue, and an observable trace store. It is not a general memory OS,
an agent framework, or an IDE. V2 adds a narrow memory subsystem only where it directly supports
agent continuity, context savings, and auditability.

V2 keeps that posture but closes three gaps that block adoption against best-in-class peers:

| #   | Pillar                     | Inspired by                                                                                                               | Goal                                                                                                                                                                                                                                                                                                                                                                  |
| --- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Stateful memory**        | [Letta](https://github.com/letta-ai/letta)                                                                                | First-class long-term memory for agents that survive across sessions: editable core memory blocks, archival passages with semantic search, sleeptime summarization.                                                                                                                                                                                                   |
| 2   | **ReasonBlocks evolution** | [reasonblocks.com](https://reasonblocks.com/) + Letta                                                                     | Ship a versioned, reviewable, retrievable corpus of reasoning procedures (already started) and add automated promotion / deprecation from production traces.                                                                                                                                                                                                          |
| 3   | **Context savings ≥ 50 %** | [Lemma](https://www.uselemma.ai/) + [Claude `/compact`](https://platform.claude.com/docs/en/build-with-claude/compaction) | Demonstrate ≥ 50 % reduction in tokens shipped to the model per task on the SWE-bench-style harness. The design adopts the stable tool-level ideas that have shown up across coding-agent optimization systems: combined search/read, batched edits, outline-first reads, deterministic SQL inspection, fuzzy edit matching, scoped recall, and lifecycle compaction. |

Each pillar is a separate work-stream. Subagents may execute packets in parallel as long as they
respect the dependency edges in [work-packets/INDEX.md](work-packets/INDEX.md).

V2 also has a routing extension recorded in
[Agent Cost-Performance Runtime](cost-performance-runtime.md) and
`docs/internal/engineering/decisions/002-quality-aware-routing.md`. Routing is deliberately treated
as a policy layer on top of the three pillars: context is reduced first, repo and procedure evidence
are retrieved second, and only then does Atelier decide whether a step can run on a cheap, mid, or
premium model tier.

---

## 2. Non-goals

- ❌ Replacing Letta. We **vendor `letta-client`** as an optional dependency and run a Letta server
  out-of-process when configured. We do not fork or copy Letta source. This keeps us on Letta's
  upgrade train forever.
- ❌ Replacing ReasonBlocks. The existing `ReasonBlock` Pydantic model in
  `src/atelier/core/foundation/models.py` is the canonical schema and stays. V2 adds adjacent
  artifacts (memory blocks, archival passages, lessons), not a rewrite.
- ❌ Tying Atelier to a specific embedding provider. The embedding client is an interface; defaults
  are local sentence-transformers if `vector` extra is installed, OpenAI if `OPENAI_API_KEY` is set,
  Letta otherwise.
- ❌ Storing chain-of-thought, secrets, or PII anywhere. The redaction filter in
  `src/atelier/core/foundation/redaction.py` extends to memory blocks and archival passages.
- ❌ Hidden-state memory. **Every memory block is reviewable, addressable, and editable** by humans
  and by the agent — the same contract ReasonBlocks already honor.
- ❌ Provider-only routing. V2 may use provider routers as execution backends, but the coding-risk
  policy lives in Atelier because it depends on ReasonBlocks, rubrics, traces, repo retrieval, and
  verifier outcomes.

---

## 3. Architecture

```
                    ┌─────────────────────────────────────────────────────┐
                    │                  Agent Host (Claude Code, Codex…)   │
                    └───────────────┬─────────────────────────────────────┘
                                    │ MCP stdio
                    ┌───────────────▼─────────────────────────────────────┐
                    │                  atelier-mcp gateway                │
                    │   (existing tools + memory, recall, routing tools)  │
                    └──┬──────────────────┬─────────────────────┬─────────┘
                       │                  │                     │
        ┌──────────────▼──────┐   ┌───────▼─────────┐   ┌───────▼─────────┐
        │ ReasonBlock Store   │   │ Memory Subsystem│   │ Lesson Pipeline │
        │ (existing FTS5 +    │   │ (NEW — Pillar 1)│   │ (NEW — Pillar 2)│
        │  pgvector optional) │   │                 │   │                 │
        │                     │   │ ┌─────────────┐ │   │ trace ingest →  │
        │ - blocks            │   │ │ Core Memory │ │   │ embed → cluster │
        │ - rubrics           │   │ │  (blocks)   │ │   │  → lesson draft │
        │ - traces            │   │ ├─────────────┤ │   │  → reviewer PR  │
        │ - environments      │   │ │  Archival   │ │   │     gate        │
        │ - failure clusters  │   │ │ (passages)  │ │   │                 │
        │                     │   │ ├─────────────┤ │   │                 │
        │                     │   │ │ Sleeptime   │ │   │                 │
        │                     │   │ │  worker     │ │   │                 │
        │                     │   │ └─────────────┘ │   │                 │
        └─────────────────────┘   └────────┬────────┘   └────────┬────────┘
                                           │                     │
                                  ┌────────▼─────────────────────▼────────┐
                                  │   Storage layer (sqlite+FTS5 default, │
                                  │   postgres+pgvector optional)         │
                                  └────────┬──────────────────────────────┘
                                           │
                  ┌────────────────────────▼───────────────────────────┐
                  │ Optional sidecar: Letta server (Docker)            │
                  │  - we talk to it via letta-client over HTTP/gRPC   │
                  │  - if absent, the in-process MemoryStore is used   │
                  │  - reachable as ATELIER_LETTA_URL                  │
                  └────────────────────────────────────────────────────┘
```

### 3.1 Two stores, one runtime

| Store           | What it holds                                                   | Authority      | Mirrored on disk?                         |
| --------------- | --------------------------------------------------------------- | -------------- | ----------------------------------------- |
| **ReasonBlock** | "**what to do**" — procedures, dead ends, rubrics, environments | reviewable PR  | Yes — `./.lessons/blocks/*.md`          |
| **Memory**      | "**what is true**" — agent state, project facts, session recall | editable in UI | Yes — `.atelier/memory/blocks/*.md` (NEW) |

**They never overlap.** A ReasonBlock can reference a memory block (via `requires_memory: [<id>]`),
but a memory block never carries procedure semantics.

### 3.2 Memory subsystem — Letta-derived schema, Atelier ownership

Letta's `Block` and `Passage` schemas are excellent starting points (see `letta.schemas.block` and
`letta.schemas.passage`). We **do not subclass them**. We derive our own minimal Pydantic models
inside `src/atelier/core/foundation/memory_models.py` (see
[IMPLEMENTATION_PLAN_V2_DATA_MODEL.md](IMPLEMENTATION_PLAN_V2_DATA_MODEL.md)) so the runtime works
without any Letta dependency at all. The Letta sidecar is opt-in and provides:

- vector storage at scale (Turbopuffer / pgvector)
- the proven `Summarizer` algorithm
- the Sleeptime agent loop

When the sidecar is configured (`ATELIER_LETTA_URL`), we proxy to it through
`src/atelier/infra/memory_bridges/letta_adapter.py` (NEW). When it is absent, the same MCP tools
work against the in-process `MemoryStore` (NEW).

### 3.3 Lesson pipeline — Lemma-style continuous learning

Atelier already records traces and detects failure clusters. V2 wires those into a **lesson
pipeline** that proposes ReasonBlock additions / edits and surfaces them in the dashboard for
human review:

```
new trace ──► fingerprint ─┬─► matches existing failure cluster ─► increments cluster
                           │
                           └─► no match
                                 │
                                 ▼
              embed (commands_run + errors_seen + diff_summary)
                                 │
                                 ▼
              k-NN against last 30 days of lesson candidates
                                 │
                                 ▼
              ┌──────────────────┴──────────────────┐
              │                                     │
        cluster size ≥ 3                  cluster size = 1..2
              │                                     │
              ▼                                     ▼
   draft LessonCandidate                  hold (stays in inbox)
   (suggested block + rubric check)
              │
              ▼
   surfaces in /learnings page (NEW)  ◄── human reviewer (one-click promote)
              │
              ▼
   atelier_extract_reasonblock + atelier_block_add  →  active ReasonBlock
```

This is a strict superset of the existing `failure_analyzer.py`. We keep that module and add a new
`lesson_promoter.py` capability that owns the pipeline, the dashboard surface, and the optional
PR-bot integration.

### 3.4 Context-savings strategy (Pillar 3)

The context-savings strategy assumes that most practical savings come from avoiding repeated
tool round-trips and repeated full-context sends, not from summarization alone. We adopt five
tool-efficiency techniques as first-class MCP tools, then layer Letta-style sleeptime
summarization and native compaction lifecycle support on top.

| Lever                                                | Owner WP | Mechanism                                                                                                                                            | Expected share |
| ---------------------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| Combined search + read                               | WP-21    | New MCP tool `search(query, path)` returns ranked snippets + content in one call. Replaces grep → read → read.                                       | ~12 %          |
| Batched edits                                        | WP-22    | New MCP tool `edit(edits=[…])` applies many edits across many files in one turn. Removes per-edit "turn-tax".                                        | ~10 %          |
| AST-aware truncation                                 | WP-11    | Extend existing `semantic_file_memory` AST capability: any file > 200 LOC returns signatures only on first read; full body only on narrow follow-up. | ~12 %          |
| Live SQL introspection                               | WP-23    | Existing `sql inspect` CLI command exposed as MCP tool `atelier sql inspect`. Single deterministic call replaces psql-via-Bash chain.                | ~5 %           |
| Fuzzy edit matching                                  | WP-24    | Extend existing `edit smart` capability: Levenshtein-tolerant `old_string` matching. Removes "old_string not found" retry loops.                     | ~6 %           |
| Sleeptime ledger summarization                       | WP-09    | Letta-style summarizer condenses tool outputs older than N events                                                                                    | ~10 %          |
| Cached tool results (`smart_read` everywhere)        | WP-10    | Existing capability promoted from optional to default; hit-rate raised via content hash                                                              | ~5 %           |
| Scoped recall from archival memory                   | WP-12    | Agent calls `memory_recall(query)` to retrieve top-3 passages instead of dumping prior trace                                                         | ~5 %           |
| **Native `/compact` lifecycle integration**          | WP-13    | At 60 % context utilisation, emit `compactd` event with preservation manifest derived from active ReasonBlocks + memory blocks                       | ~5 %           |
| Reduced ReasonBlock duplication on repeated retrieve | WP-04    | Already shipped; tuned by limit + dedup                                                                                                              | ~3 %           |
| **Total measured savings (target)**                  | —        | `benchmark-runtime --measure-context-savings` (WP-19) on Atelier's deterministic 11-prompt suite                                                     | **≥ 50 %**     |

> **Note on attribution.** These techniques are _concepts_ — we implement analogous
> behavior inside Atelier. We do not vendor plugin code or depend on third-party benchmark pages for
> CI claims. The hard claim is Atelier's own deterministic benchmark.

Each lever publishes a Prometheus metric (`atelier_tokens_saved_total&#123;lever="…"&#125;`) and is asserted
in CI under `tests/infra/test_context_savings.py`. Per-lever expected share is a _budget_,
not a commitment — overall ≥ 50 % is the only hard CI gate.

#### 3.4.1 Native `/compact` lifecycle (WP-13)

Host-native compaction often fires only after the context is already under pressure. Atelier inserts
itself earlier:

```
@ 60 % util → atelier emits monitor_event("CompactAdvised", severity="medium",
              payload=&#123;preserve: [block_ids…], pin: [memory_block_ids…],
                       open_files: [path…]&#125;)
@ 75 % util → severity="high" + suggested_compact_prompt rendered into the
              tool result so the agent sees a copy-paste compact instruction
@ post-compact → hook fires (integrations/claude/plugin/hooks/compact.py) and
                 re-injects the preservation manifest, plus runs
                 reasoning with the active task to seed
                 the new turn with the right ReasonBlocks
```

The hook already exists as a stub (`integrations/claude/plugin/hooks/compact.py`); WP-13 fills in
the body and wires the lifecycle into the in-process MCP server and the run ledger.

### 3.5 Quality-aware routing extension

Routing is not the first cost lever. It runs after context compilation, repo retrieval, memory
recall, and procedure retrieval have reduced the problem. The router classifies each agent step,
not just the top-level user request:

| Step type                                                    | Default route          |
| ------------------------------------------------------------ | ---------------------- |
| classification, summarization, bookkeeping                   | cheap                  |
| repo retrieval and scope selection                           | deterministic or cheap |
| mechanical edits with strong tests                           | cheap or mid           |
| ambiguous debugging                                          | mid or premium         |
| auth, security, payments, publishing, migrations, compliance | premium                |
| final architecture judgment                                  | premium                |

The router is verification-gated. A cheap call is counted as successful only when tests, lint,
typecheck, diff policy, rubrics, or benchmark acceptance establish the outcome. Repeated failure,
protected-file changes, contradicted repo evidence, unexpectedly large diffs, or missing verifier
coverage trigger escalation with compressed failure evidence.

Phase F in the work-packet index owns this extension:

- WP-25: routing policy configuration;
- WP-26: quality-router runtime capability;
- WP-27: verification-gated escalation;
- WP-28: routing cost-quality evals.

---

## 4. Dependency strategy

### 4.1 Letta — vendored as a client, optional extra

`pyproject.toml` gains:

```toml
[project.optional-dependencies]
memory = [
  "letta-client>=1.7.12",   # thin HTTP/gRPC client; no server pulled in
]
memory-server = [
  "letta>=0.16.7",           # only if the user wants to run Letta in-proc
]
```

Runtime imports are guarded:

```python
try:
    from letta_client import LettaClient
    _HAS_LETTA = True
except ImportError:
    _HAS_LETTA = False
```

**Never** import Letta at module top level in core code paths. Only the
`infra/memory_bridges/letta_adapter.py` imports it, and only inside class methods.

### 4.2 Embedding client

A new `src/atelier/infra/embeddings/` package wraps:

- `LocalEmbedder` — sentence-transformers (`all-MiniLM-L6-v2`, 384-d, ~80 MB) — default if `vector` extra installed
- `OpenAIEmbedder` — `text-embedding-3-small` (1536-d) — used if `OPENAI_API_KEY` and `vector` extra
- `LettaEmbedder` — proxy to Letta sidecar — used if `ATELIER_LETTA_URL`
- `NullEmbedder` — returns `[]`; archival search falls back to FTS5 only

### 4.3 Frontend

The React dashboard already exists in `frontend/src/pages/`. V2 adds three pages
(Memory, Learnings refactor, Context-Savings) and a global "Run inspector" drawer. Use existing
design-system components — do not introduce a new UI library.

---

## 5. Success metrics

These are the hard numbers V2 ships against. Every metric is a CI assertion or final proof-gate
assertion (see WP-19, WP-28, and WP-32).

| Metric                                                                 | Baseline (current `main`) |                     V2 target | Test                                                  |
| ---------------------------------------------------------------------- | ------------------------: | ----------------------------: | ----------------------------------------------------- |
| **Context tokens / SWE-bench task** (median)                           |                     ~14 k |                         ≤ 7 k | `benchmark-runtime --measure-context-savings`         |
| Cost per accepted patch                                                |                       n/a | down vs premium-only baseline | `tests/core/test_routing_evals.py`                    |
| Cheap-route success rate on low-risk tasks                             |                       n/a |                         ≥ 0.7 | `tests/core/test_routing_evals.py`                    |
| Premium escalation success after cheap failure                         |                       n/a |                         ≥ 0.6 | `tests/core/test_routing_evals.py`                    |
| Final proof report links every benchmark claim to trace evidence       |                       n/a |                          100% | `make proof-cost-quality`                             |
| Host routing enforcement is stated per host                            |                       n/a |                          100% | `tests/gateway/test_host_capability_contract_docs.py` |
| Trace confidence is stated per host                                    |                       n/a |                          100% | `tests/gateway/test_host_trace_confidence.py`         |
| Memory-block round-trip latency (p99, in-proc)                         |                       n/a |                        ≤ 5 ms | `tests/infra/test_memory_store_perf.py`               |
| Archival recall@5 on the 50-question synthetic eval                    |                       n/a |                         ≥ 0.8 | `tests/infra/test_archival_recall.py`                 |
| Lesson promotion precision on the 200-trace fixture                    |                       n/a |                         ≥ 0.7 | `tests/infra/test_lesson_promotion.py`                |
| Cold-start time (`atelier init` end-to-end)                     |                       ~2s |                          ≤ 4s | `tests/infra/test_init_perf.py`                       |
| End-to-end agent loop overhead (median, no LLM call) added by Atelier  |                    ~12 ms |                       ≤ 25 ms | `tests/infra/test_loop_overhead.py`                   |
| Frontend Lighthouse Performance score                                  |                        90 |                          ≥ 90 | `frontend/scripts/lighthouse.sh` (manual gate)        |
| `atelier verify` (lint + typecheck + tests) wall time on a fresh clone |                      ~25s |                         ≤ 60s | CI                                                    |

---

## 6. Phasing

```
Phase A — Foundation (parallel-safe)         WP-01 … WP-05    (10 days)
Phase B — Memory subsystem core              WP-06 … WP-09    ( 7 days)
Phase C — Context-savings instrumentation    WP-10 … WP-14,
                                             WP-21 … WP-24    ( 9 days)
Phase D — Lesson pipeline                    WP-15 … WP-16    ( 5 days)
Phase E — Frontend + Hosts + Docs            WP-17 … WP-20    ( 5 days)
Phase F — Quality-aware routing              WP-25 … WP-28    ( 6 days)
Phase G — Host contract + proof gate         WP-29 … WP-32    ( 4 days)
```

Total ≈ **46 dev-days** if executed sequentially; ≈ **24 dev-days** wall-time with three subagents
in parallel respecting the dependency graph (see [work-packets/INDEX.md](work-packets/INDEX.md)).

---

## 7. How a subagent should consume this plan

1. Open [work-packets/INDEX.md](work-packets/INDEX.md). Pick the **lowest-numbered packet whose
   dependencies are all satisfied** and is not already `in_progress` or `done`.
2. Open the packet file (e.g. `WP-06-memory-store.md`). The packet is self-contained — it lists
   files to create/edit, exact code stubs, and acceptance tests. Also read any architecture docs
   explicitly linked from that packet, such as [cost-performance-runtime.md](cost-performance-runtime.md)
   for routing packets.
3. Run the standing Atelier loop:
   1. `reasoning(task=<packet title>, domain="atelier.platform", files=<packet files>)`
   2. Draft a 3–8 step plan from the packet's "How to execute" section.
   3. `lint(...)` → must return `pass` or `warn` before editing.
   4. Implement the smallest diff that makes all acceptance tests pass.
   5. Run the packet's acceptance tests. If a test/command fails twice with the same signature,
      call `rescue`.
   6. `trace(...)` with `agent="atelier:code"`, `domain="atelier.platform"`,
      `status="success" | "partial"`, files_touched, output_summary referencing the WP id.
4. Mark the packet complete by setting `status: done` in the packet's front-matter and updating
   `work-packets/INDEX.md`.

Subagents must **not** invent new files outside of what a packet specifies. If a packet seems
under-specified, raise it as a comment in the packet and stop — do not improvise scope.

---

## 8. References

- [Letta architecture overview (DeepWiki)](https://deepwiki.com/letta-ai/letta) — read sections 2.3, 2.4, 3, 10.2
- [Letta GitHub (Apache 2.0)](https://github.com/letta-ai/letta) — vendored via `letta-client` only; no source copied
- [ReasonBlocks](https://reasonblocks.com/) — naming + procedure schema inspiration; existing Atelier ReasonBlock model is the authoritative implementation
- [Lemma](https://www.uselemma.ai/) — continuous-learning pipeline + automatic root-cause loop inspiration
- Existing Atelier docs: [docs/architecture/runtime.md](runtime.md), [docs/engineering/storage.md](../engineering/storage.md), [docs/engineering/architecture.md](../engineering/architecture.md)
