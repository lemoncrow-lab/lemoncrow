# Atelier Code Intelligence

## What This Is

Atelier Code Intelligence is the active brownfield program for extending Atelier's
existing CLI, MCP, service, and frontend surfaces with precomputed,
budget-aware code intelligence. It upgrades how agents find and change code so
symbol lookup, navigation, and targeted edits become near-zero-token operations
by default instead of repeated live-search work.

This project is for Atelier's agent-assisted coding workflows: first for
Atelier itself, then for repositories where Atelier-backed agents need fast,
deterministic code retrieval and editing primitives that scale better than
session-local LSP workflows.

## Core Value

Agents can find and change code through budget-aware, precomputed intelligence
with near-zero token overhead by default.

## Requirements

### Validated

- ✓ Atelier already exposes CLI, MCP, HTTP API, and frontend entry points over a
  shared runtime and persistent store — existing
- ✓ Atelier already ships MCP tools for `context`, `route`, `rescue`, `trace`,
  `verify`, `memory`, `read`, `edit`, `sql`, `code`, `search`, `compact`, and
  `shell` — existing
- ✓ `CodeContextEngine` already supports `index`, `search`, `symbol`, `outline`,
  `context`, and `impact` operations that can be extended in place — existing
- ✓ Atelier already records traces, telemetry, and smart-read savings, giving a
  baseline for measuring code-intel cost reductions — existing
- ✓ Phase 1 validated shared retrieval cache and wrapper-aware budget packing on
  existing `code` operations with `cache_hit`, `tokens_saved`, and provenance
  metadata
- ✓ Phase 1 validated routed SCIP-backed symbol intelligence behind the existing
  `code` surface with safe local fallback and cache invalidation
- ✓ Phase 1 validated hardened `code op="search"` defaults with ranked,
  snippet-free responses plus benchmark and trace evidence
- ✓ Phase 2 validated `code op="pattern"` on the existing `code` surface with
  explicit ast-grep discovery/bootstrap handling, structured rewrites, and
  benchmark evidence
- ✓ Phase 2 validated `edit kind="symbol"` on the existing `edit` surface with
  ambiguity/staleness guards plus reindex and memory follow-through
- ✓ Phase 2 validated grouped `code op="usages"` results with routed reference
  support, explicit treesitter fallback, and cache hit-rate telemetry visible
  on the Overview surface
- ✓ Phase 3 validated semantic and hybrid ranking on the existing
  `code op="search"` surface while keeping exact-name lexical behavior intact
- ✓ Phase 3 validated `memory op="recall_symbol"` with low-token default
  definition-plus-memory bundles and opt-in heavier evidence sections
- ✓ Phase 3 validated `code op="callers"` / `code op="callees"` with routed
  SCIP call-edge traversal, cheap defaults, and explicit unavailable behavior

- ✓ Phase 6 validated first-context bootstrap warming — `tool_get_context` enqueues deduped `bootstrap_context` jobs; pinned `bootstrap/<repo_id>/...` blocks warm later sessions through the existing worker path
- ✓ Phase 6 validated `scope="external"` routing — external dependency symbols carry origin metadata, and symbol-edit flows reject external targets before any file read with actionable errors
- ✓ Phase 6 validated multi-repo workspace routing — repo-aware `code op="search"` and `code op="symbol"` results carry `repo_name` metadata; additive `repo` filter narrows results without changing storage identity

### Active

- BLOC-01, BLOC-02: Block model with stability taxonomy
- COMP-01, COMP-02: Compiler core with deterministic sort and prefix boundary
- LINT-01, LINT-02: Cache-safety linter
- PROV-01, PROV-02, PROV-03: Provider adapters (OpenAI/Anthropic/Gemini/DeepSeek)
- TRAC-01, TRAC-02: Trace + telemetry integration
- CLI-01, CLI-02, CLI-03: `atelier prompt` CLI surface
- INSP-01: Session inspector
- MCP-01: MCP tool integration
- SDK-01: Python SDK public surface

### Out of Scope

- Serena or live LSP-per-session as the primary architecture — the grounded
  plan explicitly prefers precomputed artifacts over session-local language
  servers
- Replacing Atelier's existing `search` tool for text and regex workflows — it
  remains the complement when symbol-first retrieval is not the right fit
- New non-MCP delivery surfaces such as IDE plugins — the project stays within
  Atelier's current runtime, host integrations, and service/UI stack
- Full cross-language/runtime analysis beyond the planned partial static edges —
  the active plan deliberately limits this to confidence-scored common cases
- Megarepo infrastructure beyond the Zoekt-scale target — the plan explicitly
  defers ultra-large sharded search systems

## Context

This is a brownfield initialization over the existing Atelier repository. The
current product already provides a shared runtime with CLI, MCP, HTTP, and UI
entry points, plus a `CodeContextEngine` that handles indexing, symbol lookup,
outline, context packing, and impact analysis.

The project source of truth for new work is `docs/plans/active/code-intel/`,
especially `index.md` and `grounding.md`. That plan defines a full M0-M18
program focused on cost-optimal code intelligence, requires extending existing
MCP tool ops instead of adding top-level tools by default, and treats token
savings as the main justification for every milestone.

The freshly generated brownfield codebase map under `.planning/codebase/`
captures the current architecture, conventions, testing, and concerns and
should be treated as the reference for implementation planning.

## Constraints

- **Architecture**: Extend existing MCP tools and internal runtime modules
  before introducing new top-level tool registrations — `grounding.md` is the
  tie-breaker when milestone docs drift
- **Cost**: Every milestone must improve or protect token efficiency; outline
  first, cache aggressively, and make budgets explicit
- **Validation**: Milestones are not done without tests, benchmark evidence,
  validation-matrix coverage, and trace recording
- **Compatibility**: New code-intel behavior must fit Atelier's current
  Python/FastAPI/MCP/React architecture and preserve existing public entry
  points
- **Sequencing**: The full program scope is M0-M18, and the build-vs-integrate
  checkpoint in M18 must gate M16

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Extend existing `code`, `edit`, `memory`, and related MCP ops instead of adding new top-level tools | Keeps the agent surface stable and matches the grounded landing map | — Pending |
| Prefer precomputed code intelligence artifacts over live LSP/Serena-style session workflows | The north star is lower latency and lower token cost on coding tasks | — Pending |
| Treat the full `docs/plans/active/code-intel/` M0-M18 program as active scope | The user directed project initialization to follow that full plan set | — Pending |
| Define success as near-zero-token default code/search/edit flows, not feature parity with other tools | Matches both the code-intel north star and the user's definition of done | — Pending |

## Current Milestone: v1.1 Prompt Compiler

**Goal:** Make the cacheable prefix of a coding-agent prompt deterministic, large, and identical across turns — so provider-side prompt caching (OpenAI, Anthropic, Gemini, DeepSeek) fires and savings are proven in Atelier traces.

**Target features:**
- Block model with stability taxonomy (STATIC/SESSION/BRANCH/TURN/VOLATILE)
- Compiler core: deterministic sort, prefix boundary, tail budget packing
- Cache-safety linter: detects volatile-before-stable, reordered tools, timestamp leaks
- Provider adapters: OpenAI prompt_cache_key, Anthropic cache_control, Gemini, DeepSeek
- Trace integration: stable_prefix_hash, cache savings in every compile() call
- CLI surface: `atelier prompt compile|lint|inspect-session`
- Session inspector: replay JSONL sessions → diagnose cache breakers
- MCP tool: one round-trip compile + cache metadata
- Python SDK: `atelier.prompt_compiler` public surface

## Prior Milestone

**v1.0 shipped 2026-05-23.** All 7 phases complete, 21 plans, 18 requirements validated.

- All M0–M13 milestones delivered: retrieval cache, SCIP routing, hardened search, structural patterns, symbol edits, usages, semantic search, memory recall, call graph traversal, historical search, blame/churn, scale routing, cross-language edges, bootstrap warming, dependency scope, multi-repo workspaces, maintainer playbooks/scorecard.
- Symbol-first adoption playbook committed to `docs/agent-os/workflow.md`.
- ADR 001 promoted to Accepted. Scorecard metrics live. Architecture diagram published.
- Open milestones M14–M18 remain in `docs/plans/active/code-intel/index.md` for v2 consideration.

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
*Last updated: 2026-05-23 — v1.1 milestone started*
