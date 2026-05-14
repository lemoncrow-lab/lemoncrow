---
id: WP-V3.1-C
title: Sleep-time trace-consolidation worker (Letta-style background process)
phase: V3.1
boundary: Atelier-core
owner_agent: atelier:code
depends_on: [WP-V3.1-A, WP-47]
supersedes: []
status: done
---

# WP-V3.1-C — Sleep-time trace consolidation

## Why

Atelier's ReasonBlock store, lesson candidates, and memory blocks accumulate over time. V2
had no consolidation: stale lessons stay stale, near-duplicates accumulate, low-confidence
entries hang around forever, and the store gradually becomes a junkyard a reviewer
can't keep up with.

Letta solves this with a "sleep-time agent" — a background worker that runs on idle, calls
a small LLM to identify near-duplicates and outdated entries, and proposes consolidations
for human review. We adopt the **pattern**, not the code: a deterministic-where-possible,
Ollama-powered-where-helpful background process.

## Files touched

### Worker

- **NEW:** `src/atelier/core/capabilities/consolidation/__init__.py`
- **NEW:** `src/atelier/core/capabilities/consolidation/worker.py`:
  - `consolidate(*, since: timedelta, dry_run: bool = False) -> ConsolidationReport` —
    main entry point.
  - Pipeline:
    1. Load recent traces, lesson candidates, ReasonBlocks (filtered by `since`).
    2. **Deterministic pass:** find near-duplicate ReasonBlocks via cosine similarity ≥
       0.95 on their embedded body. Group as duplicate clusters.
    3. **Deterministic pass:** flag lesson candidates whose `last_seen_at` is older than
       180 days AND whose cluster has not grown in 90 days as `stale_candidate`.
    4. **Internal-LLM pass (Ollama):** for each duplicate cluster, ask Ollama:
       _"These N ReasonBlocks describe similar procedures. Are they truly duplicates? If
       so, draft a consolidated version that subsumes all. If not, list which are
       distinct."_
    5. Write everything as `ConsolidationCandidate` rows for human review (same gate as
       `LessonCandidate`).
  - On `OllamaUnavailable`, the deterministic passes still run and produce candidates with
    `method=deterministic_only`. The Ollama pass is best-effort.
- **NEW:** `src/atelier/core/foundation/models.py::ConsolidationCandidate` (new model):
  - `id`, `kind: Literal["duplicate_cluster", "stale_candidate", "low_confidence"]`,
    `affected_block_ids: list[str]`, `proposed_action: Literal["merge", "deprecate", "delete"]`,
    `proposed_body: str | None`, `evidence: dict`, `created_at`, `decided_at | None`,
    `decided_by | None`.
- **EDIT:** `src/atelier/infra/storage/sqlite_memory_store.py` — add
  `consolidation_candidate` table.
- **NEW:** `src/atelier/cli/consolidate.py` — `atelier consolidate` CLI subcommand:
  - `--since 7d` (default), `--dry-run`, `--json`
  - Prints summary: N duplicates found, M stale, K Ollama suggestions.
  - Writes candidates to DB unless `--dry-run`.
- **NEW:** MCP tools in `src/atelier/gateway/mcp_server.py`:
  - `consolidation_inbox()` — list pending `ConsolidationCandidate` rows.
  - `consolidation_decide(id, decision)` — apply or reject. Mirrors `lesson_inbox` /
    `lesson_decide` from V2 for symmetry.

### Tests + frontend hook

- **NEW:** `tests/core/test_consolidation_dedup_pass.py` — fixture with 3 near-duplicate
  ReasonBlocks; deterministic pass groups them.
- **NEW:** `tests/core/test_consolidation_stale_pass.py` — fixture with old/new candidates;
  stale pass flags correctly.
- **NEW:** `tests/core/test_consolidation_ollama_pass.py` — with mocked Ollama; cluster
  triggers Ollama call; result becomes `proposed_body` on the candidate.
- **NEW:** `tests/core/test_consolidation_ollama_unavailable.py` — Ollama down; deterministic
  candidates still ship; method recorded.
- **NEW:** `tests/gateway/test_consolidation_inbox_decide.py` — MCP smoke test for the
  inbox/decide pair.
- **EDIT (light):** `frontend/src/pages/...` — add a "Consolidations" section to the
  existing `/learnings` page (mirror lesson candidates). **Out of scope for this packet
  if it requires significant frontend rework — file as separate follow-up.**

### Scheduling

This packet does **not** ship a daemon, cron, or scheduler. The user runs `atelier
consolidate` manually or wires it into their own cron. Atelier never auto-runs background
work without explicit user invocation.

## How to execute

1. **Build deterministic passes first.** Both are pure — easy to test. Get them right
   before adding Ollama.
2. **Add Ollama pass via `internal_llm.ollama_client.chat`** with a structured JSON
   schema response (so the worker can parse "duplicate? merged body?" cleanly).
3. **Schema migration** for `consolidation_candidate`. Atelier's existing migration
   tooling handles this.
4. **CLI + MCP surfaces.** Mirror the lesson_inbox/decide pattern so the workflow is
   familiar.
5. **Defer frontend.** A "Consolidations" page section is nice-to-have. If easy, ship it.
   If it requires significant React work, file as a follow-up — don't expand this packet.

## Boundary check

The Ollama call is background-only — invoked exclusively from the `atelier consolidate`
CLI subcommand or from a cron the user wires up. **Never on the user's hot path.**
Consolidation candidates require human review before they affect the ReasonBlock /
LessonCandidate stores. No autonomous mutation.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/core/test_consolidation_dedup_pass.py \
                     tests/core/test_consolidation_stale_pass.py \
                     tests/core/test_consolidation_ollama_pass.py \
                     tests/core/test_consolidation_ollama_unavailable.py \
                     tests/gateway/test_consolidation_inbox_decide.py -v

# Manual smoke:
LOCAL=1 atelier consolidate --since 7d --dry-run --json

make verify
```

## Definition of done

- [ ] `atelier consolidate` CLI runs deterministic + Ollama passes.
- [ ] `ConsolidationCandidate` rows written; never auto-applied.
- [ ] `consolidation_inbox` / `consolidation_decide` MCP tools work.
- [ ] Ollama unavailable: deterministic candidates still produced.
- [ ] Boundary intact: Ollama only called from this CLI / cron path.
- [ ] No daemon, cron, or scheduler shipped (user wires their own).
- [ ] `make verify` green.
- [ ] V3 INDEX (V3.1 section) updated. Trace recorded.
