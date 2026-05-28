# Atelier Prompt Compiler — cache-safe context assembly

> Status: **Active** — created 2026-05-22.
> Owner: unassigned.
> ADR: TBD (`docs/decisions/002-prompt-compiler.md`).
> Tasks: see milestone files (P0–P8).

> ⚠️ **Read [`grounding.md`](grounding.md) before any milestone file.** This
> plan adds a new capability under `core/capabilities/prompt_compilation/`
> and a thin CLI surface under `gateway/adapters/cli.py`. It deliberately
> does **not** put Atelier in the LLM call path; it ships an
> analyzer/compiler/linter and an optional Python SDK that host agents
> call before they construct the prompt.

## North star

**Make the cacheable prefix of a coding-agent prompt deterministic, large,
and identical across turns, so provider-side prompt caching (OpenAI,
Anthropic, Gemini, DeepSeek) actually fires — and prove the savings in
Atelier traces.**

Every milestone is justified by one of:

1. Tokens that move from "billed every turn" to "billed once per
   session" because they sit in the cacheable prefix.
2. Cache-breaker incidents prevented (timestamps, request IDs, reordered
   tool schemas, raw test logs leaking into the prefix).
3. Observability — Atelier records `stable_prefix_hash`,
   `cacheable_tokens`, `cache_read_tokens`, etc. on every trace so the
   user can see where caching wins and loses.

## The problem in one diagram

Today, a coding-agent prompt looks like this (the **bad** layout):

```
┌────────────────────────────────────────┐
│ system prompt                          │  stable
│ current timestamp ← cache breaker      │  volatile
│ tool schemas                           │  stable
│ current repo files                     │  branch
│ user task                              │  turn
│ more tool schemas                      │  stable (poisoned by order)
│ latest terminal output                 │  volatile
│ coding rules                           │  stable
└────────────────────────────────────────┘
```

One changing value near the top poisons the entire prefix — providers
prefix-match on exact bytes, so caching gets ~0 hits.

After the compiler, the prompt looks like this (the **good** layout):

```
┌────────────────────────────────────────┐
│ tool schemas                  (static) │
│ system prompt                 (static) │
│ coding policy                 (static) │  ← cache prefix ends here
│ repo architecture summary    (session) │
│ ReasonBlocks                  (branch) │
│ stable file summaries         (branch) │
│ ──── CACHE PREFIX BOUNDARY ────        │
│ user task                       (turn) │
│ git diff                        (turn) │
│ recent tool results             (turn) │
│ latest error / test output      (turn) │
│ agent scratchpad / plan     (volatile) │
└────────────────────────────────────────┘
```

The stable prefix is identical across many turns. Providers' implicit /
explicit caches re-use it.

## Stack at a glance

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Host coding agent (Claude Code, Codex, Cursor, custom)                  │
│      │                                                                   │
│      │  pass list[PromptBlock]                                           │
│      ▼                                                                   │
│  Atelier Prompt Compiler  (core/capabilities/prompt_compilation/)        │
│                                                                          │
│      Block Registry → Stability Classifier → Prefix Planner              │
│                                                                          │
│      Cache Lint  ←  Token Estimator  →  Provider Adapter                 │
│                                                          │               │
│      Trace Recorder (stable_prefix_hash, cached_tokens) ◄┘               │
│      │                                                                   │
│      ▼                                                                   │
│   Compiled prompt + cache metadata (prompt_cache_key / cache_control)    │
│      │                                                                   │
│      ▼                                                                   │
│  Host LLM client → OpenAI / Anthropic / Gemini / DeepSeek                │
└──────────────────────────────────────────────────────────────────────────┘
```

## Block taxonomy (the data model)

```python
class Stability(str, Enum):
    STATIC   = "static"    # tool schemas, system prompt, coding policy
    SESSION  = "session"   # repo summary, project conventions
    BRANCH   = "branch"    # ReasonBlocks, file summaries for this task
    TURN     = "turn"      # user task, current diff, last tool result
    VOLATILE = "volatile"  # timestamps, request IDs, raw logs

class PromptBlock:
    id: str
    kind: BlockKind          # tool_schema | system | coding_policy |
                             # repo_summary | reasonblock | file_summary |
                             # user_task | git_diff | tool_result | scratchpad
    content: str
    stability: Stability
    cacheable: bool
    version_hash: str        # sha256(content)
    token_estimate: int
```

Sort key is `(STABILITY_ORDER[stability], kind, id)` — deterministic.

## Milestones

Each milestone has its own file. Claim one before opening the file to edit.

| ID | File | What it ships |
|----|------|---------------|
| P0 | [`P0-block-model.md`](P0-block-model.md) | `PromptBlock` dataclass, `Stability` enum, hashing, token estimator |
| P1 | [`P1-compiler.md`](P1-compiler.md) | Deterministic sort, `compile_prompt()`, prefix-boundary computation |
| P2 | [`P2-lint.md`](P2-lint.md) | Cache-safety linter: detects volatile-before-stable, reordered tools, undersized prefix |
| P3 | [`P3-providers.md`](P3-providers.md) | Provider adapters: OpenAI `prompt_cache_key`, Anthropic `cache_control`, Gemini implicit/explicit, DeepSeek hit/miss parsing |
| P4 | [`P4-cli.md`](P4-cli.md) | `atelier prompt compile|lint|inspect-session` commands |
| P5 | [`P5-trace.md`](P5-trace.md) | `PromptCompilationTrace` + integration with existing trace/telemetry pipeline; scorecard rows |
| P6 | [`P6-session-inspect.md`](P6-session-inspect.md) | Replay Claude Code / Codex session JSONL through the compiler; diagnose actual cache breakers |
| P7 | [`P7-mcp-tool.md`](P7-mcp-tool.md) | Expose compiler via the existing `compact` or a new `prompt` op on the MCP surface |
| P8 | [`P8-sdk.md`](P8-sdk.md) | Python SDK (`from atelier.prompt_compiler import …`) for host agents and custom runtimes |

## Dependency graph

```
P0 (block model)
 ├─► P1 (compiler)
 │    ├─► P2 (linter)         ← needs sorted output
 │    ├─► P3 (providers)      ← needs compiled blocks
 │    └─► P5 (trace)          ← stamps compiler output into the run ledger
 │
 ├─► P4 (CLI)                 ← thin wrapper over P1–P3
 ├─► P6 (session inspect)     ← needs P2 + P5 (replay existing sessions)
 ├─► P7 (MCP tool)            ← needs P1 + P3
 └─► P8 (SDK)                 ← needs P1 + P3; last because surface must be stable
```

Recommended build order: **P0 → P1 → P2 → P3 → P5 → P4 → P6 → P7 → P8**.

## Why this fits Atelier (and why it does not break the architecture)

1. Atelier already has the right neighbours:
   - `core/capabilities/budget_optimizer/` — knapsack packer; the compiler
     reuses it for the dynamic tail when token pressure is high.
   - `core/capabilities/context_compression/` — collapses raw tool output
     into compact summaries before they hit the tail.
   - `core/capabilities/context_reuse/` — dedups identical context across
     turns; perfect feed for `branch`-stable file summaries.
   - `core/capabilities/pricing.py` — model pricing for cache-savings math.
   - `core/capabilities/telemetry/` — trace substrate the compiler stamps.
2. **Atelier remains a provider, not an executor.** The compiler returns
   blocks + cache metadata. The host still owns the LLM call. This
   preserves the boundary that's already documented in `CLAUDE.md`
   ("MCP provider, not agent executor") and the M0 / commercial-wedge
   plans.
3. ReasonBlocks already are first-class governance objects. Promoting
   them into the **stable prefix** of every prompt is the natural next
   step — they were designed to be retrieved, verified, and audited per
   task.

## Cache-safety principles (apply to every milestone)

Any milestone that violates these needs explicit justification.

1. **Stability is a total order.** Static < session < branch < turn <
   volatile. The compiler never emits an inversion. The linter rejects
   inputs that imply one.
2. **Deterministic tool-schema ordering.** Tool schemas are sorted by
   stable id. Adding a tool mid-session is a cache miss for that turn
   and must be flagged.
3. **No timestamps, request IDs, or run IDs in the prefix.** Ever. The
   linter has a hard rule.
4. **Volatile content is summarized, not appended.** Raw test logs go
   through `context_compression` before they enter the tail.
5. **Hash everything.** Each block has `version_hash = sha256(content)`.
   The prefix hash is `sha256(version_hashes joined)`. This is what we
   record on traces — it's how we prove caching is firing.
6. **Provider rendering is mechanical.** The compiler emits a single
   canonical `CompiledPrompt`; each provider adapter is a pure function
   over it. No provider-specific reasoning in the compiler.
7. **Trace-first.** Every compile records `stable_prefix_tokens`,
   `dynamic_tail_tokens`, `cache_lint_score`, and the list of cache
   breakers found. Without traces, this feature has no economic story.

## Validation gates (cross-milestone)

Before any milestone is marked `completed`:

- Unit tests under `tests/core/capabilities/prompt_compilation/` for the
  milestone's slice.
- A row added to `docs/agent-os/validation-matrix.md`.
- A trace + scorecard row showing the optimizer's effect on a recorded
  benchmark prompt (`tests/benchmarks/prompt_compilation/`).
- Documentation in `docs/agent-os/` or `docs/architecture/` describing
  the new surface, if user-facing.

## What this is not

- **Not a new LLM client.** Atelier does not call OpenAI/Anthropic/Gemini
  /DeepSeek from the hot path. The compiler emits blocks + cache hints;
  the host sends the request.
- **Not a prompt template DSL.** Blocks are just typed text + metadata.
  No Jinja, no chains, no graphs.
- **Not a router.** Model routing already lives in
  `core/capabilities/cross_vendor_routing/` and `quality_router/`. The
  compiler hands off compiled blocks; routing decides which provider to
  send them to.
- **Not a memory store.** ReasonBlocks and semantic memory remain in
  their existing modules. The compiler reads, never writes.
- **Not the gateway/runtime venture.** The "Atelier Gateway" idea (LLM
  proxy with prompt compiler + router + guardrails + loop detector
  baked in) is a separate product line. This plan ships the analyzer +
  SDK first, which is what's compatible with Atelier today.

## Open questions

1. Where does the MCP entry point land — extend `compact` (it already
   does conversation rewriting) or introduce a new `prompt` op? Decide
   in P7 before implementation.
2. Should the SDK be vended as a separate `atelier-prompt-compiler`
   package on PyPI, or stay inside the monorepo? Decide in P8.
3. Token estimator: tiktoken-only (matches `context_compression`) or
   provider-specific tokenizers? The first call lands tiktoken; P3 can
   add provider-specific accuracy if traces show drift > 5%.
4. Do we ship a JS/TS SDK for editor-side agents? Out of scope for the
   first cut. Revisit once Python SDK has a stable surface.
