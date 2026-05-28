# Rationale — Phase-Linear Cache-Reuse for Coding Agents

This is an internal design for a token-efficiency run mode. It does not depend on
any model change; it changes how a multi-phase run is structured so the provider
prompt cache is reused and so file context carries fewer tokens.

## The two levers

### 1. Warm-prefix reuse across phases

A coding run naturally has phases: **Survey** (read the code, build context),
**Plan** (decide the change), **Implement** (apply it). A common way to build
this is one agent per phase, each with its own system prompt and a fresh
conversation. That design throws away the provider prompt cache at every phase
boundary: the Plan agent starts cold and re-ingests everything the Survey agent
read, billed at full input price.

The alternative: run the read-heavy Survey and Plan phases as **one continuous
conversation** under a **single, fixed system prompt**. Each phase is introduced
by an injected **user** message that states the current objective. Because the
conversation prefix is byte-stable, the Plan phase reads the whole Survey history
(files, search results, tool output) as a **cache hit**.

Why that saves money and time: Anthropic bills cached-prefix reads at roughly a
10× discount versus fresh input tokens, and skips recomputing the cached prefix.
The shared prefix is exactly the expensive part (the ingested codebase), so the
savings are large and the model and its answers are unchanged.

### 2. Whitespace-minified reads

Files fed into context during Survey/Plan are passed through a minifier that
collapses non-semantic whitespace (blank-line runs, trailing spaces, repeated
indentation padding) before they enter the conversation. Typical source is
~15–20% strippable whitespace, so this is a direct token cut on the read path
with no loss of meaning. The Implement phase, which edits files, reads exact
bytes instead (minification is read-context only).

## Why phases, not one blob

Keeping behavior in **per-phase user messages** rather than per-phase system
prompts is what lets the system prefix stay constant (and cacheable). A short
header at each phase boundary tells the model to set aside the previous phase's
goal and focus on the new one, which keeps a long single conversation from
drifting.

## Cache economics (the core fact)

| Token class | Relative price |
|---|---|
| Fresh input | 1.0× |
| Cache write (first time a prefix is stored) | ~1.25× |
| Cache read (prefix reused within TTL) | ~0.1× |
| Output | most expensive per token |

A phase-linear run pays one cache-write for the Survey prefix, then cache-reads
it during Plan. A per-agent run pays fresh input for the same context every
phase. On a read-heavy task the difference dominates total cost.

## What this is NOT

- Not a new or better model — quality tracks the underlying model exactly.
- Not universally better. Some tasks genuinely need a clean, divergent context
  for a sub-step, or grow a single conversation so large that the cached prefix
  itself becomes the cost. Those should fall back to the per-agent flow.

## Risks / open questions

- **Cache TTL.** The provider's prompt cache expires after a few minutes. Phase
  hand-offs must happen while the prefix is still warm, or the "reuse" silently
  becomes a cold, full-price re-ingestion. We must **measure actual cache-read
  tokens** rather than assume reuse.
- **Prefix growth.** A single conversation's prefix only grows; past a threshold
  it must be compacted (summarize prior phases, reseed, re-establish the cache).
- **Provider coupling.** Cache breakpoints are Anthropic-specific; isolate behind
  the provider adapter so other backends degrade to the per-agent flow.

## Fit with Atelier

The building blocks already exist and just need to be composed into a run mode:

| Need | Existing module |
|---|---|
| Cache breakpoint planning / diagnostics | `core/capabilities/prefix_cache/` |
| Summarize-and-reseed on prefix bloat | `core/capabilities/context_compression/`, `optimization/compaction_types.py` |
| Per-turn token/cost accounting | `core/capabilities/pricing.py`, `infra/runtime/` ledger |
| Per-phase model/route choice | `core/capabilities/model_routing/router.py` |
| Cross-run procedural reuse (complementary) | `core/capabilities/context_reuse/` |

The new piece is the phase orchestration itself; see `02-DESIGN-SPEC.md`.
