# Grounding: existing Atelier surface this plan extends

> Read this **before** any milestone file. The prompt compiler plugs into
> Atelier as a new capability under `core/capabilities/prompt_compilation/`
> and a CLI namespace under `gateway/adapters/cli.py`. It reuses the
> existing capability registry, telemetry substrate, pricing model, and
> CodeContextEngine outputs. We do not introduce a new top-level MCP
> tool unless P7 explicitly decides to.

## Architectural boundary (do not violate)

From `CLAUDE.md`:

> Boundaries:
> - no remote LLM calls in hot path
> - MCP provider, not agent executor

The compiler must respect both. It produces compiled prompts and cache
metadata; the host owns the LLM call. There are no network round-trips
inside `compile()`.

## Existing modules we reuse

| Module | Why we reuse it |
|---|---|
| `core/capabilities/budget_optimizer/` | Knapsack packer for the dynamic tail when the caller passes a `tail_budget_tokens` cap. The compiler does **not** reorder blocks itself when over budget — it asks the optimizer for a utility-maximizing subset of *turn* and *volatile* blocks, never touching the stable prefix. |
| `core/capabilities/context_compression/` | Summarizes raw test/log output into compact tail blocks. The compiler refuses to accept a `tool_result` block over a configurable token threshold (default 4,000) unless `pre_compressed=True`. |
| `core/capabilities/context_reuse/` | Deduplicates identical content across turns. The compiler asks `context_reuse` whether a candidate `file_summary` block already lives in the stable prefix from a previous turn — if yes, drop it from this turn's input. |
| `core/capabilities/code_context/` (engine) | Source of `repo_summary`, `file_summary`, and the symbol-level summaries that go into the `branch`-stable section. The engine already ships outline-first responses, which is exactly the right shape for stable file summaries. |
| `core/capabilities/pricing.py` | Per-model input / output / cached-input prices. Used to convert `cache_read_tokens` into a USD savings number on traces. |
| `core/capabilities/telemetry/` (`TelemetrySubstrate`) | The compiler's `PromptCompilationTrace` rows ride this substrate, the same way other capabilities do. |
| `core/capabilities/registry.py` (`CapabilityRegistry`) | We register `PromptCompilerCapability` here so it can be discovered by the engine and the MCP layer. |
| `infra/storage/` (SQLite store) | Persistent table for `prompt_compilations` (one row per `compile()` call) so traces and dashboards have a stable read model. |
| `core/runtime/engine.py` | Will gain a thin `compile_prompt(...)` façade once P7 wires the MCP op. Until then, the capability is callable directly via `from atelier.core.capabilities.prompt_compilation import PromptCompilerCapability`. |

## Current MCP surface (relevant tools)

Source: `src/atelier/gateway/adapters/mcp_server.py`.

| Tool | Why it matters here |
|---|---|
| `context` | Already retrieves ReasonBlocks for a task. **We will read from this**, not duplicate it. The compiler asks `context` for the relevant ReasonBlocks, marks them `Stability.BRANCH`, and includes them in the stable prefix. |
| `memory` (`recall`, `block_get`) | Source of stable team rules / governance blocks that belong in `Stability.STATIC` or `Stability.SESSION`. |
| `code` (`outline`, `context`) | Produces outline-first file summaries — perfect input for `file_summary` blocks at `Stability.BRANCH`. |
| `compact` | Closest existing tool to "rewrite my context for cache friendliness". P7 will decide whether to extend `compact` with a `mode="prompt-compile"` op or register a new `prompt` tool. |
| `trace` (`record`) | The compiler emits its own trace rows via this surface. |

## What's new (everything else)

The compiler introduces:

- `core/capabilities/prompt_compilation/__init__.py` — public surface.
- `models.py` — `PromptBlock`, `Stability`, `CompiledPrompt`,
  `PromptCompilationTrace`, `LintFinding`.
- `compiler.py` — `compile()`, `prefix_hash()`, `tail_budget_pack()`.
- `linter.py` — `lint()` with rule registry; pluggable rules.
- `providers.py` — `OpenAIRenderer`, `AnthropicRenderer`,
  `GeminiRenderer`, `DeepSeekRenderer`; all pure functions over
  `CompiledPrompt`.
- `diagnostics.py` — `inspect_session()` (P6), pretty-printers for the
  CLI.
- `capability.py` — `PromptCompilerCapability` wrapper used by the
  registry, engine, and MCP layer.

And on the gateway side:

- `gateway/adapters/cli.py` gains a `prompt` command group with
  `compile`, `lint`, and `inspect-session` subcommands (P4).

## Existing telemetry fields we extend

`core/service/telemetry/` already tracks per-call token usage including
cached input where vendors return it. We add a new row type and three
new fields to traces:

```
stable_prefix_hash: str         # sha256(joined block hashes)
stable_prefix_tokens: int
dynamic_tail_tokens: int
cache_lint_score: int           # 0–100; 100 = no cache breakers
cache_breakers: list[str]       # rule ids
estimated_cached_savings_usd: float
```

These fields land on the existing `traces` table via a new optional
JSON column (`compiler_metadata`), so we don't migrate the schema for
hosts that don't use the compiler.

## What the provider docs say (load-bearing for P3)

The compiler's provider adapters are written against the **current**
docs of each vendor. Citing these in code comments is fine; the
behaviour must match.

| Provider | Mechanism the adapter targets |
|---|---|
| OpenAI | Implicit prefix caching. Sending the same prefix consistently is sufficient; `prompt_cache_key` makes hit-rate observable and stable across servers. Minimum cacheable prefix is 1,024 tokens. The adapter computes a deterministic `prompt_cache_key` derived from the static + session block hashes; user task, diff, and tool output never enter the key. |
| Anthropic | Explicit `cache_control: {type: "ephemeral"}` on the **last** block whose prefix is identical across requests. The adapter places the breakpoint on the final block of the stable prefix (the last `branch` block) and never on a `turn` / `volatile` block. Supports up to four breakpoints; we use one by default and expose `extra_breakpoints` for tools / system if the caller wants finer control. |
| Gemini | Implicit caching on newer models; explicit `CachedContent` for guaranteed reuse. The adapter ships the stable prefix at the front and exposes a hook to register an explicit cache object when the same prefix will be reused N+ times in a session. |
| DeepSeek | Detects common prefixes automatically. Response carries `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`. The adapter parses these fields back into the trace's `cache_read_tokens` / `cache_write_tokens` so dashboards work without per-provider code paths upstream. |

If any of these docs change, update the adapter and the comment, not the
compiler core.

## Reference reading order for a new contributor

1. This file.
2. `docs/plans/active/prompt-compiler/index.md`.
3. The milestone you're claiming.
4. `src/atelier/core/capabilities/budget_optimizer/optimizer.py` —
   shows the dataclass + capability pattern this plan copies.
5. `src/atelier/core/capabilities/context_compression/capability.py` —
   shows how a capability stamps traces.
6. `src/atelier/gateway/adapters/mcp_server.py` (tool_compact and
   tool_get_context) — shows the MCP op pattern P7 will follow.
