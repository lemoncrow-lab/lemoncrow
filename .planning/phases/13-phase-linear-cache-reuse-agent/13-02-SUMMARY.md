---
phase: 13-phase-linear-cache-reuse-agent
plan: 02
subsystem: core/capabilities/context_compression + context_reuse
tags: [linear-cache-reuse, minify, reader-profile, writer-profile, telemetry, tdd]
requirements: [LINEAR-03]
dependency_graph:
  requires:
    - src/atelier/core/capabilities/context_reuse/phase_runner.py (13-01)
    - src/atelier/core/capabilities/prompt_compilation/tokens.py (estimate_tokens)
    - src/atelier/core/capabilities/context_reuse/models.py::PhaseCacheStats.minify_deltas
  provides:
    - minify_source(text, lang) -> (minified, original_tokens, minified_tokens)
    - MinificationDelta dataclass (path, lang, original_tokens, minified_tokens) + saved_tokens property + to_dict()
    - PhaseRunner._apply_read_profile() + bootstrap_reads/read_tool constructor params
  affects:
    - context_reuse/phase_runner.py (additive — read-tool bootstrap path)
tech_stack:
  added: []
  patterns:
    - "Pure-function regex-only string transform (no exec/eval/compile)"
    - "Reuses existing prompt_compilation.tokens.estimate_tokens — no new tokenizer"
    - "Profile branch (writer vs reader) in _apply_read_profile honours T-13-04 / D-09"
key_files:
  created:
    - src/atelier/core/capabilities/context_compression/minify.py
    - tests/core/test_minify_source.py
    - tests/core/test_phase_runner_minify.py
  modified:
    - src/atelier/core/capabilities/context_compression/models.py
    - src/atelier/core/capabilities/context_reuse/phase_runner.py
decisions:
  - "Token counter: alias estimate_tokens (the public symbol in prompt_compilation.tokens) as _count_tokens — single shared tokenizer per PATTERNS"
  - "bootstrap_reads constructor dict drives synthetic read-tool calls at the top of each phase agent loop (test-injectable; production wiring lands in 13-03 via runtime engine)"
  - "Conservative transform (strip trailing tabs/spaces + collapse ≥3-newline runs) applies universally; whitespace-significant set kept as the gate for any future intra-line collapses (none introduced in this plan)"
metrics:
  duration_minutes: 14
  completed: "2026-05-29"
  tasks_completed: 3
  files_created: 3
  files_modified: 2
---

# Phase 13 Plan 02: Read-Context Minifier + Reader/Writer Profile Dispatch — Summary

Read-context minifier shipped: `minify_source()` is a pure whitespace
transform (trailing WS stripped, ≥3-newline runs collapsed to two),
`MinificationDelta` records per-read token deltas, and
`PhaseRunner._apply_read_profile()` routes reader-profile reads through
the minifier while writer-profile reads remain byte-identical. All five
LINEAR-03 tests (13-02-01..05) green plus the six LINEAR-01/02 tests
from plan 01 still green.

## What Was Built

**LINEAR-03 — transform (`minify.py`):** `minify_source(text, lang) ->
(minified, original_tokens, minified_tokens)`. Module-level compiled
regexes `_BLANK_RUN` (`\n{3,}`) and `_TRAILING_WS` (`[ \t]+$`,
multiline). `_WHITESPACE_SIGNIFICANT = frozenset({"python","py","yaml",
"yml","makefile","haml"})` (case-insensitive lookup). Token counts
reuse `prompt_compilation.tokens.estimate_tokens` (aliased
`_count_tokens` at import). Pure-function: no I/O, no logging, no
mutated globals (T-13-02). Module docstring cites LINEAR-03, D-09,
D-10, D-11, T-13-02.

**LINEAR-03 — telemetry model (`models.py`):** Additive — `class
MinificationDelta` appended after `CompressionResult`; fields `path`,
`lang`, `original_tokens`, `minified_tokens`; `saved_tokens` property
= `max(0, original_tokens - minified_tokens)`; explicit `to_dict()`
matching repo convention. `DroppedContext`/`CompressionResult`
unchanged byte-for-byte.

**LINEAR-03 — dispatch (`phase_runner.py`):**

- New imports: `minify_source` and `MinificationDelta`.
- `PhaseRunner.__init__` gains two keyword-only params (defaults
  `None`): `read_tool: Callable[[str], tuple[str, str]] | None` and
  `bootstrap_reads: dict[str, list[str]] | None`.
- New private method `_apply_read_profile(phase, path, body, lang, *,
  deltas)`:
  * `phase.profile == "writer"` → returns body unchanged (T-13-04).
  * Else: calls `minify_source`, appends `MinificationDelta(...).to_dict()`
    to *deltas*, returns the minified body (D-09, D-11).
- `_run_agent_loop` now accumulates a local
  `minify_deltas: list[dict[str, Any]] = []`. If `read_tool` is wired
  and the current phase has bootstrap paths, each path is read,
  routed through `_apply_read_profile`, and appended as a
  `{"role": "tool", "name": "read", "path": ..., "content": ...}`
  message before `provider.complete`. The accumulator flows into the
  constructed `PhaseCacheStats(minify_deltas=...)`.

The `_dispatch_tool` / `_allowed_tools` plan-01 surface and the
ledger.record_call signature are untouched.

## Commits

| Hash    | Type | Description |
|---------|------|-------------|
| `503838d` | test | RED scaffolds: 5 failing tests (modules fail at import) |
| `da53d83` | feat | `minify_source` + `MinificationDelta` (LINEAR-03) |
| `d84da2b` | feat | PhaseRunner reader/writer profile dispatch + minify telemetry wiring |

## TDD Gate Compliance

* RED gate (`test(...)`): commit `503838d` introduces both test
  modules; collection fails because
  `atelier.core.capabilities.context_compression.minify` does not
  exist (intentional RED).
* GREEN gate (`feat(...)`): commit `da53d83` lands the minifier
  module + the dataclass (3 of 5 tests pass — `test_minify_source.py`
  green); commit `d84da2b` lands the PhaseRunner wiring (final 2 of 5
  tests pass — `test_phase_runner_minify.py` green).
* REFACTOR: none needed.

## Verification

```
uv run pytest tests/core/test_minify_source.py \
              tests/core/test_phase_runner_minify.py \
              tests/core/test_phase_runner.py -q
# → 11 passed in 1.57s

# Dirty-snapshot byte-equality vs plan-01 (D-18 invariant preserved):
diff -q <(git --no-pager diff -- src/atelier/core/capabilities/context_reuse/capability.py) \
        .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/context_reuse_capability.diff
diff -q <(git --no-pager diff -- src/atelier/core/runtime/engine.py) \
        .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/runtime_engine.diff
diff -q <(git --no-pager diff -- tests/core/test_capabilities_production.py) \
        .planning/phases/13-phase-linear-cache-reuse-agent/dirty-snapshots/test_capabilities_production.diff
# → all silent (byte-identical)
```

## Deviations from Plan

1. **[Rule 1 — Test fixture] YAML test fixture used a trailing `\t`** which
   PyYAML 6.0.3 rejects with `ScannerError("found character '\\t' that
   cannot start any token")`. Replaced the trailing tab with trailing
   spaces in `test_yaml_semantics_preserved`. Same coverage intent
   (trailing whitespace stripping); fixture is now parseable.
   Files modified: `tests/core/test_minify_source.py`.
   Bundled with commit `da53d83`.

2. **[Rule 3 — API shape clarification] Plan task 3 says “in the existing
   read-tool dispatch inside `_run_agent_loop`, replace the direct body
   injection” but plan 01 did not actually wire any in-loop read
   dispatch (`_dispatch_tool` is a stub that the loop never calls).
   Implemented the dispatch as a `bootstrap_reads`-driven pre-provider
   read pass — drives the same behavior the plan tests assert
   (reader/writer body delivery + per-read telemetry) without
   speculatively designing a full multi-turn tool loop (that scope
   lands in 13-03). Constructor surface extended with one extra
   keyword-only `bootstrap_reads` param (default `None`, back-compat
   preserved). The plan's `_apply_read_profile(phase, path, body,
   lang, *, stats)` signature was adapted to `*, deltas: list[...]`
   to match the actual phase-stats-construction order (stats are
   built AFTER the provider call from the planner record; the deltas
   list flows into the constructor at the end).

No Rule 4 architectural deviations.

## Pre-existing Failures (out of scope)

None checked beyond the LINEAR test scope — the two MCP test
failures noted in 13-01-SUMMARY's "Pre-existing Failures" section
remain in the same dirty `capability.py` path; this plan does not
touch that path.

## Known Stubs

None. The `read_tool` callable is `None` by default (production
wiring is the contracted scope of plan 13-03's runtime-engine
integration); this is not a stub but a deliberate deferred
dependency injection point documented in the constructor's
docstring.

## Threat Flags

None. All STRIDE entries in the plan's threat register (T-13-02,
T-13-04, T-13-SC) have explicit mitigations:

* T-13-02 — string-only regex transforms; no `exec`/`eval`/`compile`;
  asserted by `test_python_semantics_preserved` (AST equality) and
  `test_yaml_semantics_preserved` (structural equality).
* T-13-04 — `_apply_read_profile` branches on `phase.profile == "writer"`
  before calling minify; asserted by `test_writer_profile_exact_bytes`
  (byte-identity + empty `minify_deltas`).
* T-13-SC — N/A (no new dependencies).

## Self-Check: PASSED

Files exist:

```
[ -f src/atelier/core/capabilities/context_compression/minify.py ]  → FOUND
[ -f src/atelier/core/capabilities/context_compression/models.py ]  → FOUND
[ -f src/atelier/core/capabilities/context_reuse/phase_runner.py ]  → FOUND
[ -f tests/core/test_minify_source.py ]                             → FOUND
[ -f tests/core/test_phase_runner_minify.py ]                       → FOUND
```

Commits present in `git log`:

```
503838d → FOUND (RED scaffolds)
da53d83 → FOUND (minify_source + MinificationDelta)
d84da2b → FOUND (PhaseRunner reader/writer dispatch)
```
