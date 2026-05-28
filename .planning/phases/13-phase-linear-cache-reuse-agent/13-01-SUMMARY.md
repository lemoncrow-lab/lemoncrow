---
phase: 13-phase-linear-cache-reuse-agent
plan: 01
subsystem: core/capabilities/context_reuse
tags: [linear-cache-reuse, phase-runner, prefix-cache, ledger, tdd]
requirements: [LINEAR-01, LINEAR-02]
dependency_graph:
  requires:
    - src/atelier/core/capabilities/prefix_cache/planner.py
    - src/atelier/core/capabilities/prefix_cache/diagnostics.py
    - src/atelier/core/capabilities/prompt_compilation/models.py
    - src/atelier/infra/runtime/run_ledger.py (record_call)
  provides:
    - Phase, PhasePlan, PhaseResult, PhaseCacheStats, RunMode dataclasses
    - PhaseRunner orchestrator (Survey→Plan→Implement)
    - shell.md + survey/plan/implement.md fixed prompt assets
    - record_call(..., cache_write_tokens, phase) additive fields
  affects:
    - context_reuse/* (cache-warm conversation backbone for 13-02..04)
tech_stack:
  added: []
  patterns:
    - "frozen @dataclass + explicit to_dict() per repo convention"
    - "StrEnum for RunMode (no PYDantic)"
    - "Reader/Writer tool profiles via frozenset allowlist"
key_files:
  created:
    - src/atelier/core/capabilities/context_reuse/phase_runner.py
    - src/atelier/core/capabilities/context_reuse/prompts/__init__.py
    - src/atelier/core/capabilities/context_reuse/prompts/shell.md
    - src/atelier/core/capabilities/context_reuse/prompts/survey.md
    - src/atelier/core/capabilities/context_reuse/prompts/plan.md
    - src/atelier/core/capabilities/context_reuse/prompts/implement.md
    - tests/core/test_phase_runner.py
    - .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/context_reuse_capability.diff
    - .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/runtime_engine.diff
    - .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/test_capabilities_production.diff
  modified:
    - src/atelier/core/capabilities/context_reuse/models.py
    - src/atelier/infra/runtime/run_ledger.py
decisions:
  - "PhaseRunner ships in core/capabilities/context_reuse/ — gateway/CLI surfaces unchanged"
  - "shell.md is phase-agnostic; phase identity carried only in per-phase user objective"
  - "Reader profile allowlist: {read, search, glob, code_intel, web}; writer adds {write, edit, delete}"
  - "record_call(cache_write_tokens, phase) additive keyword-only with defaults — JSONL back-compat preserved"
metrics:
  duration_minutes: 75
  completed: "2026-05-29"
  tasks_completed: 3
  files_created: 10
  files_modified: 2
---

# Phase 13 Plan 01: Phase-Linear State-Machine Foundation — Summary

PhaseRunner orchestrator landed: byte-stable `shell.md` reused across
phases, Survey→Plan continuation via `continue_from`, Implement starts
lean, per-phase cache breakpoint emission, and additive
`cache_write_tokens`/`phase` ledger fields — all six LINEAR-01/02 tests
green.

## What Was Built

**LINEAR-01 — schema:** Added `Phase` (frozen), `PhasePlan` (mutable, with
`iter_order()`), `PhaseResult`, `PhaseCacheStats`, and `RunMode(StrEnum)`
to `context_reuse/models.py`. All types expose explicit `to_dict()` per
repo convention; existing `ReuseSavings`/`RankedProcedure`/`ProcedureCluster`
unchanged.

**Phase prompts:** Created `prompts/` package with four byte-stable
markdown assets. `shell.md` does not name any phase by string (D-06);
phase identity is carried only by the per-phase user objective.

Prompt SHA-256 hashes (byte stability anchor):

| File | sha256 |
|------|--------|
| `shell.md`     | `8e52903ac594bc588baa6407f36439e7d3147061bca9cc6b0b9ba74e579c349a` |
| `survey.md`    | `5d105d707b8b6029be2d53d6839e3c0501e349228b8bcdfd5c7d5d4a9276acb8` |
| `plan.md`      | `f50e6aa82e2e5b62c7949ad80b5aa0648e0305011e498e796029b72aa9d5d471` |
| `implement.md` | `240135274f64a7b82b53f2f59fe3c355a00ef974caa5879f067ec68923634167` |

**LINEAR-02 — orchestrator:** `phase_runner.PhaseRunner` loads `shell.md`
once at construction and reuses by reference (D-06 byte stability),
walks `plan.iter_order()`, and per phase:

* if `phase.continue_from is None` and `idx > 0` → reset to
  `[{system: shell}]` (D-05, lean Implement);
* otherwise append `{user: objective}` to the running list (D-04,
  Survey→Plan continuation);
* run a single-turn agent loop calling `provider.complete(messages)`;
* convert the message tail to `PromptBlock`s
  (SYSTEM/STATIC for `messages[0]`, USER_TASK/BRANCH for phase
  objectives with `stability_override_reason`, USER_TASK/TURN
  otherwise);
* call `planner.plan_with_history(..., prior_prefix_hash=diag.last_prefix_hash)`
  and `diag.record_plan(plan_record)` — one breakpoint per phase tail
  (D-07);
* emit `ledger.record_call(operation="phase:<name>", ...,
  cache_write_tokens=..., phase=phase.name, stable_prefix_hash=...,
  prefix_invalidated_reason=...)`.

**Tool-profile enforcement (D-08 / T-13-01):** Module-level
`_READER_TOOLS` and `_WRITER_TOOLS` frozensets;
`_allowed_tools(phase)` returns the correct set;
`_dispatch_tool(phase, name, payload)` raises `PermissionError`
when a write tool is requested under a reader profile (asserted by
`test_implement_starts_lean`).

**Ledger extension (T-13-04):** `RunLedger.record_call` gained
keyword-only `cache_write_tokens: int = 0` and `phase: str | None = None`,
written into the inner `tool_call` payload as `cache_write_tokens` and
`phase`. `CostTracker.record_call` is untouched. Existing
callers and on-disk JSONL records remain compatible (defaulted kwargs;
loaders must use `.get(..., default)`).

## Commits

| Hash    | Type | Description |
|---------|------|-------------|
| `12852ec` | test | RED scaffolds: six failing tests + dirty-diff snapshots |
| `4579034` | feat | LINEAR-01 schema types + phase prompt assets |
| `1e40eaf` | feat | PhaseRunner orchestrator + ledger cache_write/phase fields |

## TDD Gate Compliance

* RED gate (`test(...)`): commit `12852ec` introduces the six
  LINEAR-01/02 test functions with imports of the not-yet-existing
  `phase_runner` module — collection fails at import (intentional RED).
* GREEN gate (`feat(...)`): commit `4579034` lands the schema/prompts
  (3 of 6 tests pass); commit `1e40eaf` lands `PhaseRunner` and ledger
  fields (all 6 tests pass).
* REFACTOR: none needed.

## Verification

```
uv run pytest tests/core/test_phase_runner.py -x
# → 6 passed in 1.55s

uv run pytest tests/core/test_capabilities_production.py -q --tb=no
# → 69 passed, 5 warnings in 636.17s (matches pre-task baseline of 69 passed)

uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q --tb=no
# → 2 failed, 90 passed, 3 skipped (see "Pre-existing failures" below)

# Dirty hunk byte-equality vs snapshots:
diff -q <(git --no-pager diff -- src/atelier/core/capabilities/context_reuse/capability.py) \
        .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/context_reuse_capability.diff
diff -q <(git --no-pager diff -- src/atelier/core/runtime/engine.py) \
        .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/runtime_engine.diff
diff -q <(git --no-pager diff -- tests/core/test_capabilities_production.py) \
        .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/test_capabilities_production.diff
# → all "Files <a> and <b> differ" return nothing; byte-identical
```

## Deviations from Plan

None — plan executed exactly as written. No Rule 1-3 auto-fixes
triggered; no architectural Rule 4 decisions surfaced. Pre-commit
formatter (ruff) normalized two lines in `test_phase_runner.py` and
`phase_runner.py` (`list(messages) + [...]` → unpacking spread,
plus minor whitespace) — non-semantic.

## Pre-existing Failures (out of scope)

Two MCP regression tests failed under the full module run **only** —
they pass in isolation:

* `tests/gateway/test_mcp_tool_handlers.py::test_context_reuses_bootstrap_blocks_instead_of_enqueuing_duplicate_work`
* `tests/gateway/test_mcp_tool_handlers.py::test_context_injects_preseeded_bootstrap_blocks_without_recomputing`

Failure surface is `payload["context"] == ""` from
`tool_get_context` — touches the bootstrap warm-cache path in the
**dirty user-modified** `src/atelier/core/capabilities/context_reuse/capability.py`
(D-18). None of this plan's changes touch that code path:

* `context_reuse/phase_runner.py` is a new module not imported by
  `capability.py` or `tool_get_context`;
* `context_reuse/models.py` additions are leaf types not used by the
  bootstrap path;
* `run_ledger.record_call` additions are defaulted kwargs not invoked
  by these tests.

Verified in isolation: each test passes alone (`uv run pytest <id> -x`
→ 1 passed). The failure mode is global-state interaction across tests
in the dirty bootstrap warming code, recorded in
`deferred-items.md`. Out-of-scope per executor Scope Boundary; tracked
for the owner of the in-flight `capability.py` work.

## Known Stubs

None. `phase_runner._dispatch_tool` intentionally returns a stub
payload because no real tools are wired in plan 13-01 — multi-turn
tool dispatch and concrete tool execution land in 13-02 (reader/writer
profile minification) and 13-03 (mode dispatch). This is documented
in the module and is the contracted scope of plan 13-01.

## Threat Flags

None. All STRIDE entries (T-13-01, T-13-03, T-13-04) have explicit
mitigations covered by tests; T-13-SC (supply-chain) is N/A because
no new dependencies were added.

## Self-Check: PASSED

Files exist:

```
[ -f src/atelier/core/capabilities/context_reuse/models.py ]            → FOUND
[ -f src/atelier/core/capabilities/context_reuse/phase_runner.py ]      → FOUND
[ -f src/atelier/core/capabilities/context_reuse/prompts/shell.md ]     → FOUND
[ -f src/atelier/core/capabilities/context_reuse/prompts/survey.md ]    → FOUND
[ -f src/atelier/core/capabilities/context_reuse/prompts/plan.md ]      → FOUND
[ -f src/atelier/core/capabilities/context_reuse/prompts/implement.md ] → FOUND
[ -f src/atelier/core/capabilities/context_reuse/prompts/__init__.py ]  → FOUND
[ -f tests/core/test_phase_runner.py ]                                  → FOUND
[ -f .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/context_reuse_capability.diff ] → FOUND
[ -f .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/runtime_engine.diff ]            → FOUND
[ -f .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/test_capabilities_production.diff ] → FOUND
```

Commits present in `git log --oneline -10`:

```
12852ec → FOUND (RED scaffolds + snapshots)
4579034 → FOUND (LINEAR-01 schema + prompts)
1e40eaf → FOUND (PhaseRunner + ledger extension)
```
