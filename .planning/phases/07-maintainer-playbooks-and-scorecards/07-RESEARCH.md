# Phase 7: Maintainer Playbooks & Scorecards — Research

**Researched:** 2026-05-23
**Domain:** Documentation authorship, agent-os governance, scorecard metrics
**Confidence:** HIGH

---

## Summary

Phase 7 delivers M13, the final milestone of the Atelier code-intelligence program. M13 is explicitly designated **"Documentation only — no code changes"** in the milestone landing map (`docs/plans/active/code-intel/grounding.md`). Its entire scope is updating five existing Markdown documents, promoting one ADR from "Proposed" to "Accepted", and re-running the agent-context sync generator to propagate changes into all host instruction surfaces.

The six documents that need editing are already identified by M13: `docs/agent-os/workflow.md`, `docs/agent-os/taste-invariants.md`, `docs/agent-os/validation-matrix.md`, `docs/architecture/README.md`, `docs/quality/scorecard.md`, and `docs/decisions/001-symbol-first-mcp.md`. The content to write for each section is fully specified in `docs/plans/active/code-intel/M13-docs.md`. No new telemetry infrastructure, no Python edits, no benchmark runs are required by this phase — those were completed across Phases 1–6.

After edits, the derived host instruction surfaces (AGENTS.md, copilot-instructions.md, GEMINI.md, and integration artifacts) must be regenerated via `make sync-agent-context` and verified via `make docs-check`. This is the validation gate for the phase.

**Primary recommendation:** Treat this as three logical tasks: (1) agent guidance docs — workflow + taste-invariants, (2) measurement docs — scorecard + ADR acceptance, (3) structural docs — validation-matrix row + architecture stack diagram + sync regeneration.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Symbol-first playbook authorship | Docs (docs/agent-os/) | — | workflow.md and taste-invariants.md are the live source of truth for agent behavior rules |
| Scorecard metrics publication | Docs (docs/quality/) | Telemetry/API (existing) | scorecard.md is the definitive target; metrics are read from existing `live_savings_events.jsonl` and the Insights endpoint |
| ADR promotion | Docs (docs/decisions/) | — | status field in front-matter; no code change |
| Validation-matrix extension | Docs (docs/agent-os/) | — | New row referencing existing test commands |
| Architecture stack diagram | Docs (docs/architecture/) | — | Lift from docs/plans/active/code-intel/index.md |
| Host instruction sync | Scripts (scripts/sync_agent_context.py) | Make target | Must run after any docs/agent-os/ edit; gate: `make docs-check` |

---

## Standard Stack

### Core
| Tool | Version | Purpose | Why Standard |
|------|---------|---------|--------------|
| `make sync-agent-context` | existing | Regenerate AGENTS.md, copilot-instructions.md, GEMINI.md from docs/agent-os/ | The canonical sync mechanism; `scripts/sync_agent_context.py` reads the six DOC_LINKS defined in it |
| `make docs-check` | existing | Verify generated host files match source | Runs `test_docs.py` + `test_generated_agent_contexts.py`; phase gate |
| `uv run pytest tests/gateway/test_generated_agent_contexts.py -q` | existing | CI test confirming generated surfaces haven't drifted | Referenced in validation-matrix row for host instruction changes |

### Supporting
| Tool | Version | Purpose | When to Use |
|------|---------|---------|-------------|
| `make check-agent-context` | existing | --check mode of sync script | Quick diff-only check without writing files |
| `verify(rubric_id="rubric_source_of_truth_change", ...)` | existing | Rubric gate after host instruction changes | Required by the existing validation-matrix rule for host instruction edits |

## Package Legitimacy Audit

> Phase 7 installs **no external packages**. This section is not applicable.

---

## Architecture Patterns

### System Architecture Diagram

```
docs/agent-os/workflow.md         ─── EDIT: add Symbol-first navigation section
docs/agent-os/taste-invariants.md ─── EDIT: add 3 new code-intel invariants
docs/agent-os/validation-matrix.md─── EDIT: add M13 doc validation row
docs/quality/scorecard.md         ─── EDIT: add 5 code-intel scorecard metrics
docs/decisions/001-symbol-first-mcp.md ─── EDIT: status Proposed → Accepted
docs/architecture/README.md       ─── EDIT: lift stack diagram from index.md
         │
         ▼ make sync-agent-context
         │
AGENTS.md ─── regenerated
copilot-instructions.md ─── regenerated  
GEMINI.md ─── regenerated (if present)
integrations/claude/plugin/ ─── staging dir refreshed
.github/copilot-instructions.md ─── regenerated
.github/chatmodes/atelier.chatmode.md ─── regenerated
         │
         ▼ make docs-check
         │
tests/gateway/test_docs.py ─── PASS
tests/gateway/test_generated_agent_contexts.py ─── PASS
```

### Recommended Project Structure
```
docs/
├── agent-os/
│   ├── workflow.md          # + Symbol-first navigation section (new)
│   ├── taste-invariants.md  # + 3 new code-intel invariants (new)
│   └── validation-matrix.md # + M13 doc row (new)
├── quality/
│   └── scorecard.md         # + 5 code-intel metrics (new)
├── decisions/
│   └── 001-symbol-first-mcp.md  # status: Proposed → Accepted (change)
└── architecture/
    └── README.md            # + stack diagram block from index.md (new)
```

### Pattern 1: docs/agent-os edit → sync → verify

**What:** Edit source docs → regenerate derived surfaces → run docs-check gate
**When to use:** Every time a docs/agent-os/ file is modified
**Example:**
```bash
# Source: CLAUDE.md and docs/agent-os/README.md
# After editing any docs/agent-os/ file:
make sync-agent-context   # writes AGENTS.md, copilot-instructions.md, etc.
make docs-check           # must pass before concluding
```

### Pattern 2: ADR promotion

**What:** Change the `## Status` field in `docs/decisions/001-symbol-first-mcp.md` from "Proposed (2026-05-18)" to "Accepted (YYYY-MM-DD)" and add links to shipped benchmark evidence.
**When to use:** Once M2 + M5 are confirmed shipped (both completed in Phases 1–2 of this program).
**Example:**
```markdown
## Status

Accepted (2026-05-23)

## Evidence
- M2 symbol-search token gate: `tests/benchmarks/code_intel/test_symbol_search_bench.py`
- M5 ast-grep pattern bench: `tests/benchmarks/code_intel/test_pattern_bench.py`
- Full trace list: see docs/plans/active/code-intel/index.md
```

### Pattern 3: Scorecard metric format

**What:** Each new metric in `docs/quality/scorecard.md` needs: surface name, measurement source, current state, target, and gap.
**When to use:** Adding M13 metrics to the existing scorecard table.
**Example:**
```markdown
| Code-intel cache hit rate | C | ≥ 40% steady state | Wire `cache_hit` event count from `live_savings_events.jsonl` |
| Symbol-first adoption rate | — | ≥ 70% nav tasks | Count `code op=symbol/usages/callers/callees` vs `search` calls |
| Median tokens per nav task | — | ≤ 25% of pre-M2 baseline | Read per-session tokens from `session_stats/<uuid>.json` |
| Median tokens per refactor | — | ≤ 30% of pre-M5 baseline | Read per-session tokens from `session_stats/<uuid>.json` |
| Bootstrap cost per workspace | — | Record, no target | Read from bootstrap benchmark trace |
```

### Anti-Patterns to Avoid

- **Editing derived files directly:** AGENTS.md, copilot-instructions.md, GEMINI.md are generated. Always edit `docs/agent-os/*.md` sources and re-run `make sync-agent-context`. [VERIFIED: CLAUDE.md source-of-truth table]
- **Adding Python code in this phase:** M13 is documentation-only. No `infra/`, `core/`, or `gateway/` changes. [VERIFIED: docs/plans/active/code-intel/grounding.md milestone landing map, M13 row]
- **Adding new telemetry hooks to measure scorecard metrics:** The scorecard describes targets and how to measure them. The `live_savings_events.jsonl` + `session_stats/` infrastructure already captures `tokens_saved` and `cache_hit` per tool call. No new plumbing needed for Phase 7. [VERIFIED: CLAUDE.md data layout, src/atelier/infra/runtime/insights.py]
- **Inventing new taste invariants beyond M13's spec:** The three invariants are already written verbatim in the ADR (001-symbol-first-mcp.md enforcement section) and M13 milestone doc. Copy them exactly.
- **Forgetting to call `verify(rubric_id="rubric_source_of_truth_change", ...)` after syncing host files:** The validation-matrix requires this after host instruction source changes.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Regenerating host instruction surfaces | Hand-editing AGENTS.md | `make sync-agent-context` | Derived outputs will drift from source; `test_generated_agent_contexts.py` will fail |
| Measuring cache hit % | New telemetry infra | Existing `live_savings_events.jsonl` + `cache_hit` fields on code-intel responses | Already in place from Phase 1 M0 cache implementation |
| Describing when to use each op | Custom decision tables | The M13-specified workflow.md section rules | Exact rules already specified in M13-docs.md |

**Key insight:** Phase 7 is entirely specification-faithful documentation work. The hard decisions about tool semantics and metric targets were already made across Phases 1–6. The planner's job is to faithfully transcribe them into the five target documents.

---

## What Each Document Needs

### 1. `docs/agent-os/workflow.md` — Symbol-first navigation section

New section to append after the existing "Documentation loop" section:

```markdown
## Symbol-first navigation

When the symbol name is known, use `code` ops — not text search:

1. **Known symbol name** → `code op="symbol"` (name-first lookup). Never `code op="search"` or raw grep.
2. **"Find code that looks like X"** → `code op="pattern"` (ast-grep structural match).
3. **"Find X and everything that calls/uses it"** → `code op="symbol"` then `code op="usages"`.
4. **Refactors targeting a named symbol** → `edit kind="symbol"` or `code op="pattern" rewrite=...`. Not raw `edit` with line numbers.

Callers/callees: use `code op="callers"` / `code op="callees"` instead of reading the file and tracing manually.

Deleted or renamed symbols: use `code op="search" scope="deleted"` with optional `since=` and `touched_by=` filters.

External dependencies: use `code op="search" scope="external"`. Do not attempt `edit kind="symbol"` on external targets — the engine rejects these before any file read.

Multi-repo workspaces: add `repo=<name>` to `code op="search"` or `code op="symbol"` to scope results.
```

[VERIFIED: docs/plans/active/code-intel/M13-docs.md, grounding.md]

### 2. `docs/agent-os/taste-invariants.md` — Three new invariants

Append to the existing invariants file (these are already written verbatim in `docs/decisions/001-symbol-first-mcp.md` enforcement section):

```markdown
## Code intelligence

- *"If the caller already knows the symbol name, do not run a text search."*
- *"Default to outline-first responses. Expand only on intent."*
- *"Never edit at line numbers when the target is a named symbol."*
```

[VERIFIED: docs/decisions/001-symbol-first-mcp.md, docs/plans/active/code-intel/M13-docs.md]

### 3. `docs/quality/scorecard.md` — Five new code-intel metrics

Add five rows to the existing scorecard table:

| Metric | Target | Measurement Source |
|--------|--------|-------------------|
| % code-intel tool calls hitting cache | ≥ 40% steady state | `cache_hit` field on `code` op responses + `live_savings_events.jsonl` |
| % navigation tasks using symbol-first ops | ≥ 70% | `code op=symbol/usages/callers/callees` call count vs `search` text calls |
| Median tokens per navigation task | ≤ 25% of pre-M2 baseline | `session_stats/<uuid>.json` per-session token data |
| Median tokens per refactor task | ≤ 30% of pre-M5 baseline | `session_stats/<uuid>.json` per-session token data |
| Bootstrap cost per workspace | record, no target | `bootstrap_prefetch_bench.py` trace output |

[VERIFIED: docs/plans/active/code-intel/M13-docs.md]

### 4. `docs/decisions/001-symbol-first-mcp.md` — Promote to Accepted

Change `## Status` section from:
```
Proposed (2026-05-18)
```
to:
```
Accepted (2026-05-23)

Accepted after M2 + M5 shipped and all 18 v1 requirements completed across Phases 1–6.
Evidence: see benchmark traces in docs/plans/active/code-intel/M2-symbol-tool.md,
M5-astgrep-pattern.md, and the Phase 1–6 SUMMARY files.
```

[VERIFIED: docs/decisions/001-symbol-first-mcp.md current status, docs/plans/active/code-intel/M13-docs.md exit criteria]

### 5. `docs/agent-os/validation-matrix.md` — M13 row

Add a new row for the docs-only M13 surface:

```
| M13 agent-OS playbooks and scorecard | `make docs-check && make check-agent-context` then `verify(rubric_id="rubric_source_of_truth_change", checks={"authoritative_source_identified": True, "upstream_source_changed": True, "derived_outputs_regenerated_or_intentionally_left_unchanged": True, "contradiction_resolved_at_source": True})` |
```

[VERIFIED: docs/agent-os/validation-matrix.md existing "Host instruction sources" row pattern]

### 6. `docs/architecture/README.md` — Stack diagram

Lift the `## Stack at a glance` ASCII block from `docs/plans/active/code-intel/index.md` (the full ┌─┐ diagram showing MCP surface → SymbolIntelStore → SCIP/ast-grep/embeddings/Zoekt/Git History layers) and add it as a new "## Code Intelligence Stack" section.

[VERIFIED: docs/plans/active/code-intel/index.md "Stack at a glance", docs/plans/active/code-intel/M13-docs.md "docs/architecture/README.md" edit target]

---

## Common Pitfalls

### Pitfall 1: Editing derived host files instead of source
**What goes wrong:** AGENTS.md or copilot-instructions.md gets edited directly. `test_generated_agent_contexts.py` immediately fails because it checks that generated outputs match what `sync_agent_context.py` would produce.
**Why it happens:** AGENTS.md looks like a normal markdown file.
**How to avoid:** Only edit files in `docs/agent-os/`. Then run `make sync-agent-context`.
**Warning signs:** `make docs-check` fails with diff output.

### Pitfall 2: Skipping `make sync-agent-context` after editing workflow.md or taste-invariants.md
**What goes wrong:** The source doc is updated but the host-facing AGENTS.md and copilot-instructions.md don't reflect the new rules.
**Why it happens:** Easy to forget since editing and regenerating are decoupled steps.
**How to avoid:** Always sequence: edit → sync → check → verify rubric. Include it as an explicit task action.
**Warning signs:** `make check-agent-context` returns diff output.

### Pitfall 3: Writing new scorecard metrics without the `## Next upgrades` update
**What goes wrong:** New metrics land in the table but no "Next upgrade" items track the work needed to plumb them into the Insights tab.
**Why it happens:** Scorecard table edits feel complete on their own.
**How to avoid:** Add corresponding `## Next upgrades` bullets for any metric not yet automatically populated.

### Pitfall 4: Diverging taste-invariant copy from the ADR
**What goes wrong:** `taste-invariants.md` gets slightly different wording than `docs/decisions/001-symbol-first-mcp.md` enforcement section, creating two different normative sources.
**Why it happens:** Rewording for style during the edit.
**How to avoid:** Copy the three invariant strings verbatim from the ADR. They are already canonical.
**Warning signs:** Comparing the two files shows different wording.

### Pitfall 5: ADR promotion without linking to evidence
**What goes wrong:** Status changes to "Accepted" but no benchmark/trace citations are added. Future readers have no way to verify the acceptance was evidence-based.
**Why it happens:** Status promotion feels like a one-liner change.
**How to avoid:** Add an Evidence or Benchmark References subsection listing at least the M2, M4, M5, M6 trace IDs and benchmark files.

---

## Code Examples

### Running the sync and validation gate
```bash
# Source: CLAUDE.md common commands + docs/agent-os/validation-matrix.md
make sync-agent-context   # regenerate AGENTS.md, copilot-instructions.md, etc.
make check-agent-context  # verify regenerated == committed (diff check)
make docs-check           # also runs test_docs.py + test_generated_agent_contexts.py
```

### Validating a docs-only change per the validation-matrix rule
```bash
# Source: docs/agent-os/validation-matrix.md "Host instruction sources" row
make sync-agent-context && make check-agent-context
# then:
verify(rubric_id="rubric_source_of_truth_change", checks={
  "authoritative_source_identified": True,
  "upstream_source_changed": True,
  "derived_outputs_regenerated_or_intentionally_left_unchanged": True,
  "contradiction_resolved_at_source": True
})
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `search` tool as default for any symbol query | `code op="symbol"` as default when symbol name is known | Phase 1 M2 (2026-05-18) | ~75% token savings per navigation task |
| Raw `edit` with line numbers for symbol changes | `edit kind="symbol"` targeting named symbol | Phase 2 M4 (2026-05-19) | Atomic, boundary-safe edits; prevents stale-line-number errors |
| Text grep for structural matches | `code op="pattern"` ast-grep | Phase 2 M5 (2026-05-19) | Cross-language structural matching with optional rewrite |
| ADR status: Proposed | ADR status: Accepted | Phase 7 M13 (2026-05-23) | Documents the program as complete and decisions as settled |

**Deprecated/outdated:**
- Serena/live-LSP path: never adopted; explicitly rejected in ADR 001
- M13 docs state "Documentation only" — any Phase 7 plan adding Python code is out of scope

---

## Existing Metric Infrastructure (what scorecard metrics can read from)

The following infrastructure ships from Phases 1–6 with no Phase 7 changes needed:

| Path | Contents | Relevant Metric |
|------|----------|-----------------|
| `~/.atelier/live_savings_events.jsonl` | Per-tool-call savings events with `tokens_saved`, `lever`, `tool` | Cache hit rate, tokens saved per session |
| `~/.atelier/session_stats/<uuid>.json` | Per-session cumulative savings keyed by Claude Code UUID | Median tokens per task (read from this) |
| `~/.atelier/smart_state.json` | Cumulative savings counters | Overall adoption |
| `cache_hit` field on `code` op responses | Boolean per `code` call | Cache hit rate numerator |
| `provenance` field on `code` op responses | Which backend served the result (`scip`, `local`, `zoekt`) | Backend routing adoption |
| `src/atelier/infra/runtime/insights.py` | `InsightsWindow` aggregation of events | Powers the Insights endpoint / CLI |

**The M13 scorecard describes WHERE to read these numbers**, not how to build new plumbing.
The "Scorecard data plumbing — where do the metrics actually come from" question from M13
is answered by the above table. [VERIFIED: CLAUDE.md data layout, src/atelier/infra/runtime/insights.py]

---

## Runtime State Inventory

> Omitted — this is a greenfield docs phase, not a rename/refactor/migration.

---

## Environment Availability Audit

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `make sync-agent-context` | Host file regeneration | ✓ (Makefile target) | — | `uv run python scripts/sync_agent_context.py` directly |
| `make docs-check` | Phase gate validation | ✓ (Makefile target) | — | `uv run pytest tests/gateway/test_docs.py tests/gateway/test_generated_agent_contexts.py -q` |
| `uv` | All Python commands | ✓ | — | — |

**Missing dependencies with no fallback:** None.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (uv run pytest) |
| Config file | pyproject.toml |
| Quick run command | `make check-agent-context` |
| Full suite command | `make docs-check` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ENBL-02 | workflow.md has Symbol-first navigation section | smoke | `make docs-check` (checks regenerated contexts contain expected content) | ✅ |
| ENBL-02 | taste-invariants.md has 3 new invariants | smoke | `make docs-check` | ✅ |
| ENBL-02 | scorecard.md has code-intel metrics | manual review | `make docs-check` (structure), human review (content) | ✅ |
| ENBL-02 | 001-symbol-first-mcp.md status is Accepted | manual review | `grep "Accepted" docs/decisions/001-symbol-first-mcp.md` | ✅ |
| ENBL-02 | Generated host files updated after source edits | automated | `make check-agent-context` (exits non-zero if drift) | ✅ |

### Sampling Rate
- **Per task commit:** `make check-agent-context`
- **Per wave merge:** `make docs-check`
- **Phase gate:** `make docs-check` green + `verify(rubric_id="rubric_source_of_truth_change", ...)` passing

### Wave 0 Gaps
None — existing test infrastructure covers all phase requirements.

---

## Security Domain

> Phase 7 touches only Markdown documentation files. No authentication, session management, input validation, or cryptography surfaces are involved. Security review is not applicable to this phase.

---

## Open Questions

1. **Stack diagram scope for architecture/README.md**
   - What we know: M13 says "Lift the stack diagram from `index.md` into the canonical architecture doc"
   - What's unclear: Whether to include the full extended diagram (with Git History, cross-lang edges, etc.) or just the top-level MCP layer view
   - Recommendation: Include the full `index.md` "Stack at a glance" block. It is self-contained and adds context without requiring any maintenance until the next code-intel program.

2. **Scorecard metric for "symbol-first adoption rate" — source precision**
   - What we know: `live_savings_events.jsonl` records per-tool-call events; `code` op calls go through `session_telemetry.py`
   - What's unclear: Whether the events are granular enough to distinguish `code op="symbol"` from `code op="search"` at the event log level
   - Recommendation: In the scorecard, describe the metric as "derivable from tool-call transcripts by counting op= values" and note that precision tracking requires future telemetry work. Mark the gap in "Next upgrades".

3. **Benchmark evidence links in the ADR**
   - What we know: Trace IDs were recorded per phase (e.g., `20260519T225500-gsd-executor-7d0b2661` for M11)
   - What's unclear: Whether all M0–M13 trace IDs are easily retrievable for citation
   - Recommendation: Link to the benchmark test files (which are committed) rather than trace IDs (which are runtime artifacts). The test files serve as durable evidence anchors.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The 3 taste invariants in `docs/decisions/001-symbol-first-mcp.md` enforcement section are the exact canonical strings to copy into `taste-invariants.md` | What Each Document Needs §2 | Minor: invariant wording diverges across the two files; easy fix |
| A2 | GEMINI.md may not exist (not seen in directory listing) — only AGENTS.md and copilot-instructions.md confirmed present | Standard Stack | Low risk: `make sync-agent-context` generates whichever files are configured |
| A3 | The "Insights tab" referenced in M13 exit criteria maps to the existing `/insights` API endpoint + `insights.py` | Metric Infrastructure | Low risk: no new plumbing needed either way |

---

## Sources

### Primary (HIGH confidence)
- `docs/plans/active/code-intel/M13-docs.md` — authoritative scope, content, and exit criteria for this phase
- `docs/plans/active/code-intel/grounding.md` — milestone landing map confirming M13 is documentation-only
- `docs/decisions/001-symbol-first-mcp.md` — canonical taste invariant strings and scorecard metric definitions
- `CLAUDE.md` — data layout, sync commands, validation commands, source-of-truth table
- `scripts/sync_agent_context.py` — exact DOC_LINKS list consumed during sync (6 files)
- `docs/agent-os/validation-matrix.md` — existing row patterns and validation commands

### Secondary (MEDIUM confidence)
- `src/atelier/infra/runtime/insights.py` + `src/atelier/core/service/api.py` — confirmed `live_savings_events.jsonl` structure and token_saved field names
- `src/atelier/core/capabilities/plugin_runtime.py` — confirmed `session_stats_path`, `live_savings_events_path`, metric accumulation logic
- Phase 6 SUMMARY files (06-01, 06-02, 06-03) — confirmed no remaining Phase 7 code stubs from Phase 6

### Tertiary (LOW confidence)
- None

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — docs edit + existing make targets, fully confirmed
- Architecture: HIGH — documentation-only, no ambiguous code paths
- Pitfalls: HIGH — derived from explicit project rules in CLAUDE.md and validation-matrix

**Research date:** 2026-05-23
**Valid until:** Indefinite for a docs-only phase — no fast-moving dependencies
