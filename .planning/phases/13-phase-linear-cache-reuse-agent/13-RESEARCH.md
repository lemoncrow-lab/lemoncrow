# Phase 13: Phase-Linear Cache-Reuse Agent — Research

**Researched:** 2026-05-28
**Domain:** Prompt-cache-warm multi-phase orchestration, source minification, runtime mode selection, local benchmark proof
**Confidence:** HIGH

## Summary

Phase 13 is an **internal composition phase**, not a new-technology phase. The locked
design docs under `docs/plans/phase-linear-cache-reuse/` (index + rationale + plan +
design-spec) fully specify the run mode: a declarative Survey → Plan → [review] →
Implement state machine, where Plan continues the Survey conversation
(`continue_from="survey"`) under one fixed system prompt so the provider prompt cache
reads the Survey prefix as a hit; Implement starts as a lean separate writer step
fed only the accepted plan text; the read-only profile feeds files through a
`minify_source()` whitespace stripper; the runtime gains `linear | per_agent | auto`
mode dispatch; and a local benchmark over ≥7 scenarios proves ≥30% cost / ≥25%
wall-time reduction at equal-or-better task success.

All scaffolding the design references already exists: `prefix_cache/planner.py`
and `prefix_cache/diagnostics.py` produce cache-breakpoint and hit-ratio telemetry;
`model_routing/router.py` (Phase 12) emits cache-aware `RouteDecision`s with
`cache_cost_usd` and sticky routing; `context_compression/capability.py` provides
the summarize-and-reseed primitive for prefix bloat; `infra/runtime/run_ledger.py`
already records `input_tokens`, `output_tokens`, `cache_read_tokens`,
`stable_prefix_hash`, and `prefix_invalidated_reason` per LLM call. The new code
is small and surgical: a `PhaseRunner`, a fixed `prompts/` set, a
`minify_source()` helper, dataclass additions to `context_reuse/models.py`,
mode-dispatch wiring in `AtelierRuntimeCore`, and a benchmark suite.

**Primary recommendation:** Plan the work as nine task chunks that mirror
`01-PLAN.md`'s nine implementation steps verbatim. Treat the existing dirty work
in `context_reuse/capability.py`, `runtime/engine.py`, and
`tests/core/test_capabilities_production.py` as additive only — Wave 0 must
snapshot the dirty diff and the planner must forbid any task from overwriting
the modified hunks. Make the benchmark (LINEAR-05 / TBEVAL-01) a first-class
task, not a follow-up.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Locked Plan Shape**
- **D-01:** Treat `docs/plans/phase-linear-cache-reuse/` as the locked design input. Downstream agents must follow the rationale, plan, and design spec before inventing alternatives.
- **D-02:** Minimal viable version is exactly the design spec's three load-bearing pieces: Survey→Plan continuation, phase behavior via injected user messages over one fixed system prompt, and minified reads during Survey/Plan.
- **D-03:** Implement and test in `core/capabilities/context_reuse/` and `core/runtime/engine.py`; gateway surfaces stay thin dispatchers only.

**Conversation and Cache Semantics**
- **D-04:** Survey and Plan share one message list. Plan must use `continue_from="survey"` and must not re-open a fresh conversation for already-read Survey context.
- **D-05:** Implement does not continue Survey/Plan history by default. It receives the accepted plan and starts lean with writer tools and exact-byte reads.
- **D-06:** Keep one fixed system prompt across phases. Phase-specific behavior lives in small injected user objective messages, not per-phase system prompts, so the cacheable prefix remains stable.
- **D-07:** Set/record cache breakpoints at phase tails and record actual cache-read/cache-write/fresh-input/output token counts. Do not assume cache reuse from structure alone.

**Tool Profiles and Minified Reads**
- **D-08:** Reader profile is read/search/code-intel only; writer profile adds edit/write/delete capabilities. The read-only grant structurally enforces plan-before-mutation.
- **D-09:** `minify_source()` is a read-context optimization only. Writer paths must read exact bytes.
- **D-10:** Preserve Python/YAML semantics by using conservative line trimming for whitespace-significant formats; more aggressive whitespace collapse is allowed only for languages where it is safe and tested.
- **D-11:** Record original vs minified token counts per read/phase so the benchmark can attribute savings to minification separately from cache reuse.

**Mode Selection and Fallbacks**
- **D-12:** Add explicit modes `linear`, `per_agent`, and `auto`. `auto` chooses linear only for context-sharing scenarios with projected prefix under threshold.
- **D-13:** Fall back to `per_agent` for divergent sub-contexts, oversized prefixes, or providers where cache-breakpoint semantics are unavailable or unmeasurable.
- **D-14:** Cache TTL and prefix bloat are first-class guards. If a handoff is too slow or prefix too large, either record a cold-read/fallback or compact/reseed with explicit evidence.

**Benchmark and Proof**
- **D-15:** The Phase 13 benchmark must cover at least seven representative scenarios and report cost, wall time, cache-hit/cache-read ratio, minification delta, and task success.
- **D-16:** Success target is ≥30% lower cost and ≥25% lower wall-time at equal-or-better task success on context-sharing scenarios.
- **D-17:** Benchmark artifacts belong under `docs/plans/phase-linear-cache-reuse/` or the phase directory and must separate cache-reuse savings from minification savings.

**Existing Dirty Work**
- **D-18:** Current uncommitted changes in `src/atelier/core/capabilities/context_reuse/capability.py`, `src/atelier/core/runtime/engine.py`, and `tests/core/test_capabilities_production.py` are treated as user/ongoing work. Executors must inspect and preserve them; do not overwrite or "fix" them just to make tests pass.

### the agent's Discretion
The planner may choose the exact dataclass field names, runner API shape, and benchmark fixture mechanics as long as they satisfy the locked docs, requirements, and validation targets above.

### Deferred Ideas (OUT OF SCOPE)
None — discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| LINEAR-01 | `context_reuse` defines `Phase`, `PhasePlan`, `PhaseResult`, cache-stat fields, and a declarative Survey → Plan → Implement state machine | Extend `context_reuse/models.py` (currently 62 lines, dataclasses with `to_dict()`) with `Phase`, `PhasePlan`, `PhaseResult`, `PhaseCacheStats`. Mirror the JSON schema in `02-DESIGN-SPEC.md §1`. |
| LINEAR-02 | Survey and Plan run under one fixed system prompt with per-phase user objectives; Plan uses `continue_from="survey"` so provider cache can read the Survey prefix warm | New `PhaseRunner` + `prompts/{survey,plan,implement}.md` + shared shell prompt. Cache breakpoint per phase via existing `PrefixCachePlanner.plan_with_history()`. |
| LINEAR-03 | Read-only Survey/Plan tool profile uses safe source minification and records original vs minified token counts; writer profile reads exact bytes | New `minify_source(text, lang)` in `context_compression/`; reader profile invokes it on read tool returns, writer profile bypasses it. Token deltas captured via `count_tokens` from `core/foundation/retriever`. |
| LINEAR-04 | Runtime exposes `linear \| per_agent \| auto` mode selection with an auto heuristic that avoids linear mode for divergent or oversized contexts | Add `RunMode` enum and dispatch in `AtelierRuntimeCore`. `auto` consults projected prefix tokens (PrefixCachePlanner) + Phase 12 `RouteDecision.cache_cost_usd` to decide. |
| LINEAR-05 | Linear-vs-per-agent benchmark proves ≥30% lower cost and ≥25% lower wall-time at equal-or-better success on context-sharing scenarios | New `benchmarks/linear_vs_per_agent/` suite under `benchmarks/` (existing `benchmarks/ab/`, `benchmarks/terminalbench/` patterns); 7 fixture scenarios; report under `docs/plans/phase-linear-cache-reuse/results/`. |
| TBEVAL-01 | Local benchmark artifact records cost, latency, cache-hit ratio, minification delta, and task success for ≥7 representative scenarios | Same suite as LINEAR-05 — single artifact satisfies both. Separate minification delta from cache-reuse delta per D-17. |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Phase state machine + `PhaseRunner` | core/capabilities/context_reuse | core/runtime/engine | Capability owns the loop; engine dispatches mode (per CLAUDE.md invariant) |
| Fixed shell + phase objective prompts | core/capabilities/context_reuse/prompts | — | Co-located with the runner that loads them |
| `continue_from` message-list sharing | core/capabilities/context_reuse (PhaseRunner) | core/capabilities/prefix_cache | Runner owns the message list; prefix_cache emits breakpoint diagnostics |
| Cache breakpoint + diagnostics | core/capabilities/prefix_cache | infra/runtime (ledger) | Already exists (planner + diagnostics) — reuse, don't duplicate |
| `minify_source()` | core/capabilities/context_compression | — | Compression capability already owns context-reduction primitives |
| Prefix-bloat compaction/reseed | core/capabilities/context_compression | core/capabilities/context_reuse (caller) | `compress_with_provenance` + sleeptime path already exists |
| Mode dispatch (`linear \| per_agent \| auto`) | core/runtime/engine | core/capabilities/model_routing | Engine routes; model_routing supplies `cache_cost_usd` for `auto` heuristic |
| Per-call cache/cost telemetry | infra/runtime/run_ledger | core/capabilities/pricing | `record_call()` already records `cache_read_tokens`, `stable_prefix_hash`, `prefix_invalidated_reason`; may need `cache_write_tokens` + `phase` fields |
| Benchmark harness | benchmarks/linear_vs_per_agent (new) | benchmarks/ab (pattern source) | Co-located with existing ab/terminalbench harnesses |
| Gateway/CLI/MCP exposure of `--mode` | gateway/cli, gateway/adapters/mcp_server | — | Thin dispatcher per CLAUDE.md |

## Standard Stack

### Core (already in repo — reuse, do not introduce new libs)

| Module | Path | Purpose | Why Standard |
|--------|------|---------|--------------|
| `PrefixCachePlanner` / `PrefixCachePlan` | `core/capabilities/prefix_cache/planner.py` | Compute `prefix_hash`, `prefix_tokens`, `dynamic_tokens`, `invalidated_reason` per turn | Already the cache-breakpoint anchor per D-07 |
| `PrefixCacheDiagnostics` / `PrefixTurnRecord` | `core/capabilities/prefix_cache/diagnostics.py` | Per-session `cache_hit_ratio`, `cache_read_tokens_saved`, invalidation list | Source of truth for "did the cache actually hit" per D-07 |
| `ContextCompressionCapability` | `core/capabilities/context_compression/capability.py` | `compress_with_provenance(ledger, token_budget, task)` + sleeptime summary | Already the compaction/reseed path per design §5 mapping table |
| `ModelRouter.recommend()` | `core/capabilities/model_routing/router.py` | Phase 12 cache-aware decision with `cache_cost_usd`, `route_decision`, sticky window | Feeds `auto` heuristic per D-12 |
| `RunLedger.record_call()` | `infra/runtime/run_ledger.py` | Records `input_tokens`, `output_tokens`, `cache_read_tokens`, `stable_prefix_hash`, `prefix_invalidated_reason`, `cost_usd` | All cache stats per D-07 already flow through this; add `cache_write_tokens` + `phase` |
| `count_tokens` | `core/foundation/retriever.py` | Estimator for minify_source token deltas | Already used across capabilities |

### Supporting

| Module | Path | Purpose | When to Use |
|--------|------|---------|-------------|
| `compile_prompt` | `core/capabilities/prompt_compilation/compiler.py` | Block-level prompt assembly with stability tagging | If the shared shell prompt is assembled from typed blocks (recommended for cache-safety) |
| `PromptBlock`, `Stability`, `BlockKind` | `core/capabilities/prompt_compilation/models.py` | STATIC/SESSION/BRANCH/TURN/VOLATILE classification | Tag the fixed system prompt STATIC so PrefixCachePlanner anchors it |
| `bench.mode.is_off` | `src/atelier/bench/mode.py` | Bench-mode toggle (Phase 1) | Benchmark fixtures honor on/off arms |
| Existing benchmark harnesses | `benchmarks/ab/runner.py`, `benchmarks/terminalbench/agent_adapter.py` | A/B run + claude `-p` subprocess invocation patterns | Copy structure for `linear_vs_per_agent` suite |

### Alternatives Considered

| Instead of | Could Use | Tradeoff | Decision |
|------------|-----------|----------|----------|
| Hand-roll minifier | `python-minifier` (PyPI) | External dep + Python-only + does too much (renames symbols, evaluates exprs) — unsafe for *read context* preservation | **Use hand-rolled whitespace stripper.** D-10 mandates conservative line trimming; we want zero semantic transformation, just whitespace. ~30 LoC. |
| Hand-roll mode enum | `enum.StrEnum` | Stdlib | **Use `StrEnum`** (Python 3.11+). Project uses Python 3.12. |
| New cache-stats dataclass | Extend `PrefixCacheDiagnostics` directly | Adds non-prefix concerns to a focused class | **Add `PhaseCacheStats` in `context_reuse/models.py`** wrapping diagnostics output + minification deltas + per-phase token splits. |
| Per-phase system prompts | One fixed system prompt + user-message phase headers | Per-phase prompts invalidate cache at every boundary (the bug we're fixing) | **D-06 locks the single-prompt approach.** |

**Installation:** No new external packages. (Confirm in Wave 0; the design needs nothing beyond what `pyproject.toml` already pins.)

**Version verification:** N/A — no new packages.

## Package Legitimacy Audit

No new external packages are added by this phase. All implementation reuses existing in-repo modules and the standard library. The Package Legitimacy Gate is satisfied vacuously.

## Architecture Patterns

### System Architecture Diagram

```
                          ┌──────────────────────────────────────┐
                          │ gateway/cli  │  gateway/mcp_server   │
                          │   (thin)     │     (thin)            │
                          └──────────┬───────────┬───────────────┘
                                     │  mode={linear|per_agent|auto}
                                     ▼
                          ┌──────────────────────────────────────┐
                          │ AtelierRuntimeCore  (engine.py)      │
                          │   mode dispatch + existing surfaces  │
                          └──────────┬───────────────────────────┘
                                     │
                ┌────────────────────┼─────────────────────────┐
                │                    │                         │
                ▼ linear             ▼ per_agent (existing)    ▼ auto
       ┌─────────────────┐                              ┌──────────────┐
       │  PhaseRunner    │◄─────────────────────────────┤ heuristic:   │
       │  (context_reuse)│                              │ prefix_size, │
       └──┬──────────────┘                              │ divergence,  │
          │                                             │ cache_cost   │
          │  one fixed system prompt (STATIC block)     │ (ModelRouter)│
          │  + user objective per phase                 └──────────────┘
          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ SURVEY (reader profile)                                     │
   │   read-tools → minify_source() → append to messages         │
   │   PrefixCachePlanner.plan_with_history → breakpoint @ tail  │
   │   RunLedger.record_call(phase="survey", cache_*_tokens=...) │
   └────────────────┬────────────────────────────────────────────┘
                    │ continue_from="survey"  (same messages list)
                    ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ PLAN (reader profile, warm cache hit on prior prefix)       │
   │   no re-read of files already in history                    │
   │   breakpoint @ plan tail; record cache_read_tokens          │
   │   emits PhaseResult.plan_text                               │
   └────────────────┬────────────────────────────────────────────┘
                    │ accepted plan text only (no message history)
                    ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ IMPLEMENT (writer profile, lean fresh conversation)         │
   │   exact-byte file reads (NO minify_source)                  │
   │   write/edit/delete enabled                                 │
   └─────────────────────────────────────────────────────────────┘

   Compaction guard (every phase tail):
     if prefix_tokens > threshold:
         ContextCompressionCapability.compress_with_provenance() → reseed
         PrefixCachePlanner records new prefix_hash + invalidated_reason
```

### Recommended Project Structure

```
src/atelier/core/capabilities/context_reuse/
├── capability.py         # existing (dirty work in progress — do not overwrite)
├── models.py             # EXTEND: add Phase, PhasePlan, PhaseResult, PhaseCacheStats, RunMode
├── phase_runner.py       # NEW: PhaseRunner class
├── prompts/              # NEW
│   ├── __init__.py
│   ├── shell.md          # fixed system prompt (STATIC)
│   ├── survey.md         # phase objective (injected as user message)
│   ├── plan.md           # phase objective
│   └── implement.md      # phase objective
└── ...

src/atelier/core/capabilities/context_compression/
├── capability.py         # existing
├── minify.py             # NEW: minify_source(text, lang) + token-delta helpers
└── models.py             # EXTEND: MinificationDelta dataclass (optional)

src/atelier/core/runtime/
└── engine.py             # EXTEND: RunMode dispatch (preserve dirty work)

src/atelier/infra/runtime/
└── run_ledger.py         # EXTEND (carefully): add cache_write_tokens + phase fields to record_call

benchmarks/linear_vs_per_agent/    # NEW
├── __init__.py
├── runner.py             # mirrors benchmarks/ab/runner.py
├── scenarios.yaml        # 7 fixtures
└── reporter.py           # cost/wall-time/cache-ratio/minify-delta table

tests/core/
├── test_phase_runner.py              # NEW (LINEAR-01, LINEAR-02)
├── test_phase_runner_minify.py       # NEW (LINEAR-03)
├── test_runtime_mode_dispatch.py     # NEW (LINEAR-04)
└── test_minify_source.py             # NEW (LINEAR-03 semantics)

docs/plans/phase-linear-cache-reuse/
├── 00-rationale.md                   # existing (locked)
├── 01-PLAN.md                        # existing (locked)
├── 02-DESIGN-SPEC.md                 # existing (locked)
└── results/                          # NEW (LINEAR-05, TBEVAL-01)
    └── linear_vs_per_agent.md
```

### Pattern 1: Phase State Machine

**What:** Declarative dict-of-steps with `kind` (`agent | gate | side_effect`), `profile` (`reader | writer`), optional `continue_from`, optional `objective` path.
**When to use:** Always — this is the canonical shape per `02-DESIGN-SPEC.md §1`.
**Example:**
```python
# Source: docs/plans/phase-linear-cache-reuse/02-DESIGN-SPEC.md §1
@dataclass(frozen=True)
class Phase:
    name: str                          # "survey" | "plan" | "implement"
    kind: Literal["agent", "gate", "side_effect"]
    profile: Literal["reader", "writer"]
    objective_path: str | None         # "prompts/survey.md"
    continue_from: str | None = None   # "survey" for plan phase
    next: str | None = None

@dataclass
class PhasePlan:
    name: str
    entry: str
    phases: dict[str, Phase]

@dataclass
class PhaseResult:
    phase_name: str
    messages: list[dict[str, Any]]     # full message list at phase tail
    cache_stats: PhaseCacheStats
    output_text: str                   # e.g., plan markdown for "plan" phase
```

### Pattern 2: `continue_from` Message-List Sharing

**What:** When the runner enters a phase with `continue_from="survey"`, it does **not** instantiate a fresh `messages = [{"role": "system", ...}]`. It mutably extends the prior phase's list with `{"role": "user", "content": <plan objective>}`. The provider sees a byte-identical prefix → cache hit.
**Anti-pattern:** Serializing/deep-copying the message list per phase. Even whitespace drift in the system prompt evicts the cache.

### Pattern 3: Stability Tagging for Cache Anchoring

**What:** The fixed shell prompt is registered as a `PromptBlock(stability=Stability.STATIC, kind=BlockKind.SYSTEM)`. Phase objective injections are `Stability.BRANCH` (per-phase) user messages. `PrefixCachePlanner.plan_with_history()` then emits a stable `prefix_hash` for the system prompt and surfaces `invalidated_reason` if anything STATIC drifts.
**Why:** Existing infra (`prefix_cache/planner.py`) does the hashing/diagnostics for free if you classify blocks correctly.

### Pattern 4: Reader vs Writer Tool Profile

**What:** Per-phase tool allowlist; reader = {read, search, glob, code-intel, web}; writer = reader + {write, edit, delete}. The runner asserts before each tool call that the active tool is in the active phase's allowlist.
**Why:** Structurally enforces "plan before mutation" per D-08.

### Anti-Patterns to Avoid

- **Per-phase system prompts** (D-06): kills cache at every boundary — exact bug being fixed.
- **Minifying writer-profile reads** (D-09): writer needs exact bytes for edits — minify drift could produce wrong-line patches.
- **Aggressive whitespace collapse on Python/YAML** (D-10): leading indentation is semantic. Stripping or collapsing it breaks the file.
- **Assuming cache hit from structure** (D-07): always cross-check `cache_read_tokens` from the provider response against expected; provider TTL may have elapsed.
- **Re-reading files already in Survey history during Plan** (D-04 implication): defeats the entire warm-prefix purpose. The plan objective prompt must explicitly say "do not re-read what's already in history".
- **Per-phase fresh conversation for Implement that drags Survey/Plan history along** (D-05): wastes tokens; Implement should be lean and only receive the accepted plan text.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Prefix-hash + cache-breakpoint computation | New hasher | `PrefixCachePlanner.plan_with_history()` | Already handles BlockKind/Stability classification, invalidation reasons, token estimates |
| Cache-hit-ratio aggregation | Inline counters | `PrefixCacheDiagnostics` | Existing per-turn record + ratio + invalidation list |
| Ledger token/cost recording | New event type | `RunLedger.record_call()` | Already has `cache_read_tokens`, `stable_prefix_hash`, `prefix_invalidated_reason`, `cost_usd` |
| Prefix-bloat compaction | New summarizer | `ContextCompressionCapability.compress_with_provenance()` + `compress_with_sleeptime()` | Already exists |
| Cache-cost-aware route decision | New scorer | `ModelRouter.recommend()` (Phase 12) — returns `cache_cost_usd` | The `auto` heuristic should call this, not duplicate logic |
| Python source minification beyond whitespace | `python-minifier`, `pyminify`, AST round-trip | **Don't.** Hand-roll a ~30-line conservative whitespace stripper. | Read-context safety per D-10; aggressive minifiers rename symbols and break semantics |
| YAML "minification" | Any YAML lib round-trip | Conservative trailing-whitespace + blank-line collapse only | YAML is whitespace-significant; round-tripping reorders keys |
| Benchmark scenario harness | New harness from scratch | Copy `benchmarks/ab/runner.py` + `benchmarks/terminalbench/agent_adapter.py` patterns | Existing claude `-p --output-format stream-json` capture already extracts `cache_creation_input_tokens` + `cache_read_input_tokens` (TB-02) |

**Key insight:** Phase 13 is overwhelmingly a *composition* phase. The two pieces of genuinely new code are (a) the `PhaseRunner` loop (~150-250 LoC) and (b) `minify_source()` (~30-60 LoC). Everything else is wiring.

## Runtime State Inventory

Phase 13 is largely additive (new files + extensions). Limited runtime-state surface:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — no schema change to `~/.atelier/runs/<session_id>.json` is strictly required, but `record_call()` may add `cache_write_tokens` + `phase` fields. Old run-ledger JSON remains readable (additive only). | None for migration; document new fields. Verified via `infra/runtime/run_ledger.py` `record_call` & `snapshot` paths. |
| Live service config | None — feature ships entirely in-process. | None. |
| OS-registered state | None. | None. |
| Secrets/env vars | None new. Existing `ATELIER_ROOT`, `ATELIER_BENCH_MODE` already governing. Mode flag `--mode linear|per_agent|auto` may surface as CLI flag and/or `ATELIER_RUN_MODE` env var (planner's discretion). | Document if new env var added. |
| Build artifacts | None — `uv sync` not required because no new dependencies (confirm in Wave 0). | None. |

## Common Pitfalls

### Pitfall 1: Cache TTL Race
**What goes wrong:** Survey → Plan transition takes long enough (human gate, slow tool call, slow provider) that the provider's prompt cache evicts the Survey prefix. Plan then silently pays fresh-input prices for everything — the "cache hit" is fictional.
**Why it happens:** Anthropic's prompt cache TTL is ~5 minutes (per `00-rationale.md §Risks`).
**How to avoid:** Implement the warmth guard from `01-PLAN.md` Step 4. Track wall-clock since last call per session; if a phase handoff would land outside TTL window, either issue a tiny keep-alive call or log a cold-read event in the ledger.
**Warning signs:** `cache_read_tokens == 0` on the first turn of Plan when `prefix_hash` matches Survey — provider is the source of truth, not our structural model.

### Pitfall 2: System-Prompt Drift Invalidating Cache
**What goes wrong:** Someone "improves" the shell prompt by appending dynamic info (date, model name, session id). Every session now writes a fresh prefix; cache hit ratio drops to zero.
**Why it happens:** The shell prompt looks like the right place for "context".
**How to avoid:** Classify the shell prompt as `Stability.STATIC` via `PromptBlock`; let `PrefixCachePlanner` surface `prefix_invalidated_reason="system_prompt_changed"` in CI. Unit test asserts the rendered system prompt is byte-identical across two distinct PhaseRunner sessions with different inputs.
**Warning signs:** `PrefixCacheDiagnostics.invalidations` non-empty across sessions; `invalidated_reason == "system_prompt_changed"`.

### Pitfall 3: Minify Drops Python Indentation
**What goes wrong:** A minifier collapses leading whitespace runs; Python files become syntactically invalid; the model can no longer reason about scoping.
**Why it happens:** Reuse of a generic whitespace-collapse routine intended for JSON/HTML.
**How to avoid:** Language-dispatch in `minify_source(text, lang)`. For Python and YAML, only trim *trailing* whitespace and collapse *consecutive blank lines* (≥3 → 1). Never touch leading whitespace.
**Warning signs:** Unit test compiles minified Python (`ast.parse`) and re-parses minified YAML (`yaml.safe_load`); any failure = regression.

### Pitfall 4: Implement Phase Re-Reads Files Already in Survey/Plan
**What goes wrong:** Implement is a separate writer agent without the Survey history. If the implement objective doesn't list the accepted plan + critical files, the writer may re-read everything from scratch — losing the cost savings.
**Why it happens:** D-05 cuts message history; implement must instead receive a compact "carry pack" (plan text + critical-files list).
**How to avoid:** Plan phase must emit a "critical files" list as part of its output; PhaseRunner feeds that list into Implement's objective. (Per `02-DESIGN-SPEC.md §3 plan.md` bullet — *"end with a short critical files list"*.)
**Warning signs:** Implement phase `input_tokens` ≈ Survey + Plan input_tokens (writer paid for re-ingestion).

### Pitfall 5: `auto` Picks `linear` for a Divergent Task
**What goes wrong:** `auto` greedily picks linear; sub-step needs a clean context; long mixed conversation confuses the model; quality regresses.
**Why it happens:** Heuristic only looks at prefix size, not context divergence.
**How to avoid:** Include a divergence signal in the heuristic — e.g., if requested step type is "research a different codebase" vs "implement the current plan", fall back to `per_agent`. Phase 12 `ModelRouter.recommend()` already encodes task-type/step-type — reuse.
**Warning signs:** Benchmark scenario fixtures must include at least one divergent-context case; `auto` must pick `per_agent` for it (LINEAR-04 acceptance).

### Pitfall 6: Overwriting Dirty User Work in `capability.py` / `engine.py` / `test_capabilities_production.py`
**What goes wrong:** A task naively rewrites these files to a "clean" version; user's in-progress changes lost.
**Why it happens:** Per D-18, three files have uncommitted modifications (verified via `git status` 2026-05-28). A planner unaware of this may emit "replace function X" tasks that wipe them.
**How to avoid:** Wave 0 must (a) `git stash` or snapshot the diff for these three files, (b) plan additive changes only — new methods, new fields — not rewrites of modified hunks. Verifier checks `git diff` reduces only by *intentional additions*, not by reverting dirty hunks.
**Warning signs:** A `git diff` line count *decreases* against the snapshotted dirty baseline.

## Code Examples

### Pattern: Fixed Shell Prompt as STATIC Block (sketch)
```python
# Source: derived from 02-DESIGN-SPEC.md §3 + prompt_compilation/models.py
from atelier.core.capabilities.prompt_compilation.models import (
    PromptBlock, Stability, BlockKind,
)

SHELL_PROMPT = (Path(__file__).parent / "prompts" / "shell.md").read_text()

def shell_block() -> PromptBlock:
    return PromptBlock(
        kind=BlockKind.SYSTEM,
        stability=Stability.STATIC,
        content=SHELL_PROMPT,
    )

def phase_objective_block(phase_name: str) -> PromptBlock:
    text = (Path(__file__).parent / "prompts" / f"{phase_name}.md").read_text()
    return PromptBlock(
        kind=BlockKind.USER,
        stability=Stability.BRANCH,
        content=text,
    )
```

### Pattern: PhaseRunner Skeleton (sketch)
```python
# Source: synthesized from 02-DESIGN-SPEC.md §1–§4, 01-PLAN.md Step 3
class PhaseRunner:
    def __init__(self, plan: PhasePlan, *, provider, ledger: RunLedger,
                 planner: PrefixCachePlanner, diag: PrefixCacheDiagnostics): ...

    def run(self) -> dict[str, PhaseResult]:
        results: dict[str, PhaseResult] = {}
        messages: list[dict] = [{"role": "system", "content": SHELL_PROMPT}]
        for phase_name in self._phase_order():
            phase = self.plan.phases[phase_name]
            if phase.continue_from is None:
                # fresh: implement starts here with carry-pack
                messages = [{"role": "system", "content": SHELL_PROMPT}]
            messages.append({"role": "user", "content": load_objective(phase)})
            messages, phase_stats, output_text = self._run_agent_loop(
                phase, messages,
            )
            # breakpoint @ phase tail
            plan_record = self.planner.plan_with_history(
                blocks=_to_blocks(messages),
                prior_prefix_hash=self.diag.last_prefix_hash,
            )
            self.diag.record_plan(plan_record)
            results[phase_name] = PhaseResult(
                phase_name=phase_name,
                messages=messages.copy(),
                cache_stats=phase_stats,
                output_text=output_text,
            )
        return results
```

### Pattern: minify_source (sketch)
```python
# Source: synthesized from 02-DESIGN-SPEC.md §5 + D-10
import re

_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)

_WHITESPACE_SIGNIFICANT = {"python", "py", "yaml", "yml", "makefile", "haml"}

def minify_source(text: str, lang: str) -> tuple[str, int, int]:
    """Conservative read-context minifier.

    Returns (minified_text, original_token_estimate, minified_token_estimate).
    NEVER touches leading whitespace for whitespace-significant languages.
    """
    original = text
    out = _TRAILING_WS.sub("", text)
    out = _BLANK_RUN.sub("\n\n", out)
    if lang.lower() not in _WHITESPACE_SIGNIFICANT:
        # safe to collapse intra-line whitespace runs not inside string literals
        # (caller may extend; for safety v1, do nothing extra)
        pass
    return out, count_tokens(original), count_tokens(out)
```

### Pattern: Mode Dispatch in Engine (sketch)
```python
# Source: synthesized from 01-PLAN.md Step 7 + D-12
from enum import StrEnum

class RunMode(StrEnum):
    LINEAR = "linear"
    PER_AGENT = "per_agent"
    AUTO = "auto"

class AtelierRuntimeCore:
    def run_phased(self, *, mode: RunMode = RunMode.AUTO, ...) -> dict:
        chosen = self._resolve_mode(mode, projected_prefix_tokens, divergence_signal)
        if chosen is RunMode.LINEAR:
            return self.phase_runner.run(...)
        return self._run_per_agent(...)  # existing path

    def _resolve_mode(self, mode, prefix_tokens, divergence) -> RunMode:
        if mode is not RunMode.AUTO:
            return mode
        if divergence or prefix_tokens > LINEAR_PREFIX_THRESHOLD:
            return RunMode.PER_AGENT
        rec = self.model_router.recommend(...)  # Phase 12 cache-aware
        if rec.cache_cost_usd > rec.estimated_quality_gain_usd:
            return RunMode.PER_AGENT
        return RunMode.LINEAR
```

## State of the Art

| Old Approach | Current Approach | Why |
|--------------|------------------|-----|
| One agent per phase, fresh conversation each time | One conversation Survey→Plan + lean writer Implement | Provider prompt cache reuses Survey prefix at ~10× discount |
| Per-phase system prompts | Fixed system prompt + per-phase user objective messages | Per-phase system prompts evict the cache at every boundary |
| Ingest exact file bytes into read context | Minify whitespace before ingest on read profile | 15–20% token cut on read path with no semantic loss |
| Hand-rolled cache hit detection | Already-built `PrefixCacheDiagnostics` + provider `cache_read_input_tokens` | Phase 12 telemetry pipeline already wires both |

**Deprecated/outdated:** None in this phase. All Atelier surfaces involved are current (Phase 12 just landed).

## Project Constraints (from CLAUDE.md)

- **Python via `uv run` only.** All test/lint/typecheck commands MUST start `uv run pytest …`, `uv run mypy …`, etc. Direct `python3` calls fail or use wrong env.
- **Architectural invariant:** `gateway/ → core/ → infra/`. New capability code lives in `src/atelier/core/capabilities/context_reuse/`. `gateway/cli/`, `gateway/adapters/mcp_server.py`, and `gateway/adapters/runtime.py` stay thin dispatchers.
- **Generated files (`AGENTS.md`, `copilot-instructions.md`, host instruction files) are NEVER edited directly** — edit `docs/agent-os/*.md` and run `make sync-agent-context`. Phase 13 does not need to touch any of these.
- **Public data contracts:** typed dataclasses (or Pydantic) with explicit `to_dict()`. Pattern verified in existing `context_reuse/models.py` and `context_compression/models.py`.
- **Test gate:** `make pre-commit` (format + lint + typecheck + docs + test). Per-task verify uses `uv run pytest -q -x -m "not slow"` for fast feedback; phase gate runs full `make test`.
- **Pre-existing failures policy:** broad typecheck/test failures may be pre-existing; do not fix unrelated breakage to make tests pass (especially relevant given dirty work in `capability.py`).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Adding `cache_write_tokens` + `phase` keyword args to `RunLedger.record_call()` is non-breaking (existing callers don't pass them). | Standard Stack / RunLedger | Low — kwargs with defaults are additive. Planner must verify all `record_call` call sites still pass kwargs. |
| A2 | Anthropic prompt-cache TTL is ~5 min, breakpoints are Anthropic-specific. | Pitfall 1, D-13 | Cited from project's own `00-rationale.md`. If TTL changes, warmth-guard threshold needs retuning — won't break the design. |
| A3 | `python-minifier` is unsafe for read-context preservation because it renames symbols. | Don't Hand-Roll | If wrong, we have a more powerful option; not blocking. |
| A4 | No new pip packages required. | Standard Stack | Confirm in Wave 0 by running `uv pip list` against the design needs; if a YAML round-trip is needed for testing, `pyyaml` is already a transitive. |
| A5 | The dirty work in `capability.py` (verified `git diff` shows ~23 line modification around the retrieval payload) is genuinely user/in-progress work to preserve, not a regression to discard. | Pitfall 6 | If user intended to discard, planner should ask in `gsd-discuss-phase` — but D-18 already locks "preserve". |
| A6 | `ModelRouter.recommend()` returns a `ModelRecommendation` with `cache_cost_usd` and `estimated_quality_gain_usd_estimated` fields suitable for the `auto` heuristic. | Mode Dispatch code example | Verified via `grep` on `router.py` lines 103-275 (cache_cost_usd field exists). Field name for quality gain may differ — planner confirms in Wave 0. |

## Open Questions (RESOLVED)

1. **Exact compaction threshold and TTL keep-alive policy** (acknowledged by `01-PLAN.md` as "settle empirically in Step 9").
   - What we know: prefix bloat past a threshold should trigger `compress_with_provenance` + reseed; cache TTL ~5 min triggers warmth guard.
   - What's unclear: numeric thresholds (e.g., 60k tokens? 100k?), keep-alive call shape (1-token ping? cache-only block re-emit?).
   - Recommendation: Land defaults (e.g., 80k prefix tokens, 4-min TTL guard) as constants in `phase_runner.py`, tunable via env vars; document defaults in benchmark report; tune from benchmark data.
   - **RESOLVED:** Plan 13-03 Task 2 lands `LINEAR_PREFIX_THRESHOLD = 60_000` as a module constant in `engine.py` (used by `_resolve_run_mode` AUTO heuristic). Compaction-threshold and TTL keep-alive constants live in `phase_runner.py` (Plan 13-01) with documented defaults; both are revised post-benchmark in Plan 13-04 Task 3 (manual sign-off step). D-14 telemetry (cache_read/write/phase) captured in every run feeds the tuning loop.

2. **Does `record_call()` need `cache_write_tokens` as a new field, or is the existing `input_tokens` adequate?**
   - What we know: D-07 requires recording fresh-input / cache-write / cache-read / output explicitly. Today's ledger has `input_tokens` + `cache_read_tokens` only.
   - What's unclear: provider response gives `cache_creation_input_tokens` (TB-02) — we likely need a dedicated field to avoid double-counting.
   - Recommendation: Add `cache_write_tokens: int = 0` to `record_call()` keyword args; back-compat preserved.
   - **RESOLVED:** Plan 13-01 adds `cache_write_tokens: int = 0` kwarg to `RunLedger.record_call()` (back-compat default). Consumed by both `PhaseRunner` (linear arm) and `_run_per_agent` (per-agent arm) in Plan 13-03 Task 2, and surfaced in the 13-04 benchmark cell payload (`test_runner_records_required_fields` pins the field).

3. **Where does the `phase` label live in the ledger?**
   - Recommendation: Add `phase: str = ""` kwarg to `record_call()`; tag every call within `PhaseRunner` with `phase="survey" | "plan" | "implement"`.
   - **RESOLVED:** Plan 13-01 adds `phase: str = ""` kwarg to `RunLedger.record_call()`. `PhaseRunner.run()` tags every linear-arm call with the active phase name; `_run_per_agent` (Plan 13-03 Task 2) tags each per-phase provider call with `phase=phase.name`. Plan 13-03 Task 1 test `test_per_agent_writes_ledger` asserts the per-agent arm writes one ledger row per phase with distinct phase labels.

4. **Should `auto` heuristic also consider Phase 12 sticky routing?**
   - Recommendation: Yes — if `ModelRouter.recommend()` returns a sticky-window remaining count, the heuristic should respect it (don't break a sticky session for a mode switch). Cross-check with Phase 12 plan in `12-01-SUMMARY.md`.
   - **RESOLVED (deferred):** Phase 13 ships the prefix-tokens + divergence-signal AUTO heuristic only (Plan 13-03 `_resolve_run_mode`). Sticky-routing awareness is explicitly deferred — Plan 13-03 Task 2 leaves a TODO comment in `_resolve_run_mode` citing this question and PATTERNS line 303. D-14 telemetry (phase-tagged ledger rows + cache_read/write counts) provides the data needed to re-evaluate the heuristic once benchmark results land; a follow-up phase can extend `_resolve_run_mode` without breaking the signature.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12 + `uv` | All Python code/tests | ✓ (verified — pyproject.toml + .venv) | uv-managed | — |
| `pytest` | Unit tests | ✓ | per pyproject | — |
| `mypy --strict` | `make typecheck` | ✓ | per pyproject | — |
| `claude -p` CLI | Benchmark scenarios (real-LLM arm) | ✓ already used in `benchmarks/terminalbench/agent_adapter.py` (TB-02) | TB-02 verified | Skip real-LLM arm in CI; use mock-provider arm for unit/integration |
| Anthropic API key (`ANTHROPIC_API_KEY`) | Benchmark real-LLM runs | varies — env-specific | — | Benchmark must skip-with-message when key absent; CI relies on mock provider |
| `pyyaml` | minify_source YAML tests | ✓ (transitive — used elsewhere) | — | — |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback:** ANTHROPIC_API_KEY in CI — benchmark guards itself; unit tests use a fake provider.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | `pytest` (via `uv run pytest`) |
| Config file | `pyproject.toml` (existing pytest config) |
| Quick run command | `uv run pytest -q -x -m "not slow"` |
| Full suite command | `make test` (or `uv run pytest -q`) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| LINEAR-01 | `Phase`, `PhasePlan`, `PhaseResult`, `PhaseCacheStats` dataclasses present with `to_dict()` | unit | `uv run pytest tests/core/test_phase_runner.py::test_models_have_required_fields -x` | ❌ Wave 0 |
| LINEAR-01 | Declarative state machine matches design-spec schema | unit | `uv run pytest tests/core/test_phase_runner.py::test_state_machine_schema -x` | ❌ Wave 0 |
| LINEAR-02 | Survey + Plan share ONE message list (Plan does not reset) | unit | `uv run pytest tests/core/test_phase_runner.py::test_plan_continues_survey_messages -x` | ❌ Wave 0 |
| LINEAR-02 | One fixed system prompt across phases (byte-identical) | unit | `uv run pytest tests/core/test_phase_runner.py::test_system_prompt_byte_stable -x` | ❌ Wave 0 |
| LINEAR-02 | Cache breakpoint set at each phase tail | unit | `uv run pytest tests/core/test_phase_runner.py::test_breakpoint_per_phase_tail -x` | ❌ Wave 0 |
| LINEAR-02 | Implement starts lean (no Survey/Plan history) | unit | `uv run pytest tests/core/test_phase_runner.py::test_implement_starts_lean -x` | ❌ Wave 0 |
| LINEAR-03 | `minify_source()` collapses non-significant whitespace | unit | `uv run pytest tests/core/test_minify_source.py::test_collapses_blank_runs -x` | ❌ Wave 0 |
| LINEAR-03 | Python semantics preserved (ast.parse succeeds) | unit | `uv run pytest tests/core/test_minify_source.py::test_python_semantics_preserved -x` | ❌ Wave 0 |
| LINEAR-03 | YAML semantics preserved (yaml.safe_load succeeds, identical structure) | unit | `uv run pytest tests/core/test_minify_source.py::test_yaml_semantics_preserved -x` | ❌ Wave 0 |
| LINEAR-03 | Writer profile bypasses minify (exact bytes returned) | unit | `uv run pytest tests/core/test_phase_runner_minify.py::test_writer_profile_exact_bytes -x` | ❌ Wave 0 |
| LINEAR-03 | Original-vs-minified token counts recorded per read | unit | `uv run pytest tests/core/test_phase_runner_minify.py::test_minify_telemetry -x` | ❌ Wave 0 |
| LINEAR-04 | `RunMode.LINEAR` / `PER_AGENT` / `AUTO` enum + dispatch | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_explicit_modes -x` | ❌ Wave 0 |
| LINEAR-04 | `auto` picks linear for context-sharing under threshold | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_auto_picks_linear -x` | ❌ Wave 0 |
| LINEAR-04 | `auto` falls back to per_agent for oversized prefix | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_auto_falls_back_oversized -x` | ❌ Wave 0 |
| LINEAR-04 | `auto` falls back to per_agent for divergent contexts | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_auto_falls_back_divergent -x` | ❌ Wave 0 |
| LINEAR-05 / TBEVAL-01 | Benchmark runs ≥7 scenarios | integration | `uv run pytest benchmarks/linear_vs_per_agent/tests/test_runner.py -q` | ❌ Wave 0 |
| LINEAR-05 / TBEVAL-01 | Report records cost, wall-time, cache-hit ratio, minify delta, task success | integration | `uv run pytest benchmarks/linear_vs_per_agent/tests/test_reporter.py -q` | ❌ Wave 0 |
| LINEAR-05 | Linear ≥30% cheaper, ≥25% faster on context-sharing scenarios | benchmark artifact (slow, manual) | `uv run python -m benchmarks.linear_vs_per_agent.runner --out docs/plans/phase-linear-cache-reuse/results/` | ❌ Phase tail |
| (regression) | Existing `tests/core/test_capabilities_production.py` (with dirty additions) still passes | regression | `uv run pytest tests/core/test_capabilities_production.py -q` | ✓ (file exists, dirty) |
| (regression) | `tests/core/test_code_context.py` unaffected | regression | `uv run pytest tests/core/test_code_context.py -q` | ✓ |
| (regression) | MCP/gateway surfaces unbroken | regression | `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q` | ✓ |

### Sampling Rate
- **Per task commit:** `uv run pytest -q -x -m "not slow"` (entire fast suite — Atelier convention)
- **Per wave merge:** `make lint && make typecheck && make test`
- **Phase gate:** `make pre-commit` green + benchmark artifact under `docs/plans/phase-linear-cache-reuse/results/` showing the LINEAR-05 deltas

### Wave 0 Gaps

- [ ] `tests/core/test_phase_runner.py` — covers LINEAR-01, LINEAR-02 (state machine, continue_from, breakpoints, system-prompt stability, lean implement)
- [ ] `tests/core/test_phase_runner_minify.py` — covers LINEAR-03 reader/writer profile distinction + telemetry
- [ ] `tests/core/test_minify_source.py` — covers LINEAR-03 semantic-preservation guarantees (Python, YAML, generic)
- [ ] `tests/core/test_runtime_mode_dispatch.py` — covers LINEAR-04 enum + heuristic
- [ ] `benchmarks/linear_vs_per_agent/tests/` — minimal integration tests for runner+reporter shape (fast; no real LLM calls)
- [ ] Fake-provider fixture for PhaseRunner tests (records messages it would send, returns scripted responses with explicit `cache_read_tokens` / `cache_creation_input_tokens` to exercise telemetry)
- [ ] Snapshot of dirty diffs for `context_reuse/capability.py`, `runtime/engine.py`, `tests/core/test_capabilities_production.py` (so verifier can detect if any task wipes them)

## Security Domain

Phase 13 is internal orchestration with no new external surface, no auth, no user-data flow, no crypto, no new IO endpoints. Most ASVS categories are non-applicable.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | — (no new auth surface) |
| V3 Session Management | no | — |
| V4 Access Control | yes (internal) | Reader-vs-writer tool profile is structural access control. PhaseRunner MUST assert tool ∈ profile_allowlist before any tool invocation; reject otherwise. |
| V5 Input Validation | yes | `minify_source(text, lang)` must validate `lang` against allowlist; unknown lang → safest path (trailing-ws + blank-run only); never `eval`/`exec` source. |
| V6 Cryptography | no | — |
| V8 Data Protection | yes (logging) | Ledger now captures per-phase prompt/response excerpts via existing `record_call(prompt=, response=)`. Phase 13 must NOT widen what's logged (no source-code bodies dumped wholesale into ledger snapshots beyond existing patterns). |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Writer profile activated in wrong phase (Survey/Plan accidentally mutates files) | Tampering | Per-phase tool allowlist asserted by PhaseRunner before dispatch; unit test verifies reader profile rejects write/edit/delete |
| Prompt-injection in read context (a `# eval(input())` style comment in a minified file) | Tampering | Minifier does NOT evaluate; only string transforms. Source IDs (file path) carried as separate metadata, not embedded in trusted system prefix. |
| Cache poisoning via shared message list | Tampering | Single conversation; system prompt is STATIC + hashed; any drift surfaces `prefix_invalidated_reason`. Cache scope is per-process / per-provider session, not shared across users. |
| Telemetry leakage (full file contents in run ledger persist to `~/.atelier/runs/`) | Information Disclosure | `RunLedger.record_call(prompt=, response=)` already serialises; Phase 13 keeps same behavior. Optionally elide minified-content with a hash if size > threshold (planner discretion). |
| Bench-mode arm leakage (LINEAR benchmark cross-contaminates with `ATELIER_BENCH_MODE=off`) | Spoofing | Reuse Phase 1 pattern: each arm in separate `ATELIER_ROOT`; benchmark runner asserts bench mode read once before any module imports. |

## Sources

### Primary (HIGH confidence)

- `docs/plans/phase-linear-cache-reuse/index.md` — locked thesis
- `docs/plans/phase-linear-cache-reuse/00-rationale.md` — cache economics, two levers, risks
- `docs/plans/phase-linear-cache-reuse/01-PLAN.md` — nine implementation steps + validation + success criteria
- `docs/plans/phase-linear-cache-reuse/02-DESIGN-SPEC.md` — state machine schema, continue_from semantics, phase objectives, tool profiles, mode mapping
- `.planning/REQUIREMENTS.md` (LINEAR-01..05, TBEVAL-01) — acceptance requirements
- `.planning/phases/13-phase-linear-cache-reuse-agent/13-CONTEXT.md` — locked discussion decisions D-01..D-18
- `.planning/phases/12-cache-aware-routing/12-01-SUMMARY.md` — Phase 12 outputs (cache_cost_usd, sticky routing, RouteDecision telemetry)
- `CLAUDE.md` — `uv run` mandate, gateway/core/infra invariant, capability placement rule
- Source files inspected directly:
  - `src/atelier/core/capabilities/context_reuse/models.py` (62 LoC — dataclass pattern)
  - `src/atelier/core/capabilities/prefix_cache/planner.py` (129 LoC — `PrefixCachePlanner.plan_with_history`)
  - `src/atelier/core/capabilities/prefix_cache/diagnostics.py` (111 LoC — `PrefixCacheDiagnostics`)
  - `src/atelier/core/capabilities/context_compression/capability.py` (255 LoC — `compress_with_provenance`)
  - `src/atelier/core/capabilities/context_compression/models.py` (60 LoC — `CompressionResult`)
  - `src/atelier/core/capabilities/model_routing/router.py` (494 LoC — `ModelRouter.recommend`, `cache_cost_usd`)
  - `src/atelier/core/runtime/engine.py` (952 LoC, dirty — `AtelierRuntimeCore`)
  - `src/atelier/infra/runtime/run_ledger.py` (469 LoC — `record_call` signature)
- Verified dirty work via `git status -s` and `git diff --stat`:
  - `src/atelier/core/capabilities/context_reuse/capability.py` (M, +23/-12 around retrieval payload empty-case)
  - `src/atelier/core/runtime/engine.py` (M, +18/-3 around `get_context` empty-payload short-circuit)
  - `tests/core/test_capabilities_production.py` (M, +66/-1 added coverage)

### Secondary (MEDIUM confidence)

- TB-02 acceptance (Anthropic stream-json fields: `cache_creation_input_tokens`, `cache_read_input_tokens`) from `.planning/REQUIREMENTS.md`
- Existing benchmark harness structure: `benchmarks/ab/runner.py`, `benchmarks/terminalbench/agent_adapter.py`

### Tertiary (LOW confidence)

- Numerical defaults for compaction threshold (~80k tokens) and TTL keep-alive (~4 min) — admitted open per `01-PLAN.md` "settle empirically".

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every cited module verified by direct file read; locked design names them explicitly.
- Architecture: HIGH — `02-DESIGN-SPEC.md` is unambiguous; mapping table in `00-rationale.md` confirms module homes.
- Pitfalls: HIGH (TTL, system-prompt drift, minify safety, dirty work) — explicitly listed in locked docs or verified via `git status`.
- Pitfalls: MEDIUM (Implement re-reads, auto divergence) — derived from design spec, no in-repo evidence yet.
- Validation Architecture: HIGH — matches `01-PLAN.md` verify steps verbatim.

**Research date:** 2026-05-28
**Valid until:** 2026-06-27 (30 days — stable internal composition; locked design)
