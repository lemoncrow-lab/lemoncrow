# Research notes — what the providers actually do

> Background reference for P3. Keep these notes close to the adapter
> code so future contributors can re-verify against current docs.

## Why prefix caching exists at all

Every major provider has a flavor of prompt caching because they share
the same problem: long-context agentic loops re-send 80–95% of the
same bytes on every turn. Recomputing KV cache for those bytes is
wasted GPU time and wasted billing. So providers built a server-side
prefix store keyed on the leading bytes of the request, and **the
prefix only hits if the leading bytes are byte-for-byte identical to a
recent request**.

That's the entire reason the prompt-compiler exists: coding agents
naturally inject changing bytes near the top (timestamps, request IDs,
reordered tool schemas, raw test logs). One stray byte in the
prefix = 0 cache hit.

The compiler enforces a **total order** on stability tiers so the
leading bytes are stable across turns.

## Provider behaviour summary

### OpenAI

- **Mechanism:** implicit prefix caching (no opt-in required) and an
  observability hook called `prompt_cache_key`.
- **Floor:** caching activates at ≥1,024 tokens of shared prefix.
- **Hit visibility:** response includes `usage.prompt_tokens_details.cached_tokens`.
- **Implication for us:** the adapter must keep the prefix
  byte-identical and ≥1,024 tokens, and emit a stable
  `prompt_cache_key` derived from the static block hashes (so OpenAI
  routes the request to a server that has the cache warm).

### Anthropic

- **Mechanism:** explicit `cache_control: {type: "ephemeral"}` on a
  message-content block.
- **Floor:** Anthropic enforces minimum cacheable prefix sizes (varies
  by model; usually around 1,024 tokens for Sonnet-class, 2,048 for
  Haiku-class — confirm against current docs at adapter-write time).
- **Hit visibility:** response includes
  `usage.cache_creation_input_tokens` and `usage.cache_read_input_tokens`.
- **Implication for us:** the adapter places `cache_control` on the
  **last** block of the stable prefix. We never put a breakpoint on a
  block whose content changes turn-to-turn. Anthropic supports up to
  four breakpoints — we default to one and expose extras for callers
  who want finer-grained reuse (e.g. one on tools, one on system, one
  on file-summaries).

### Gemini

- **Mechanism:** implicit caching on newer models (auto-detected at
  the server), plus an explicit `CachedContent` API for guaranteed
  reuse with TTL.
- **Hit visibility:** response includes
  `usage.cached_content_token_count`.
- **Implication for us:** the default adapter just puts the stable
  prefix first. The opt-in explicit mode returns a payload the host
  can use to create a `CachedContent` object out-of-band.

### DeepSeek

- **Mechanism:** automatic prefix detection.
- **Hit visibility:** response includes `usage.prompt_cache_hit_tokens`
  and `usage.prompt_cache_miss_tokens`.
- **Implication for us:** no special request fields are needed; the
  adapter just parses the usage payload. This is also the cheapest
  test case for the trace-attach machinery — DeepSeek will hit cache
  on a second identical request with no client-side opt-in.

## Why a coding-agent prompt is the worst case

A coding agent emits prompts that look like this every turn:

```
system prompt (stable)
[tool schemas, occasionally reordered when a tool is added]
[repo summary]
[user task — changes per task]
[git diff — changes every commit]
[tool results — accumulates, each turn appends raw output]
[scratchpad — agent's own free-form notes]
```

Three things destroy caching:

1. **Out-of-order tool schemas.** Adding a tool mid-task shifts every
   following byte; the cache evicts.
2. **Turn-content above tool-content.** Many SDKs/agents put the user
   task before tool schemas in the rendered messages. The first
   user-task byte is now the prefix boundary; tools no longer cache.
3. **Volatile content in the prefix.** Timestamps and request IDs
   embedded in "system" blocks for debugging poison the entire prefix.

The compiler's sort order + linter cover all three. P6's
session-inspector demo demonstrates them on a real Claude Code session.

## What we deliberately avoid

- **Inventing a cache.** Atelier does **not** cache LLM responses. We
  only help the provider's cache fire. Adding our own response cache
  would duplicate infrastructure (and break the "no remote LLM calls in
  hot path" boundary because we'd want to swap stale results in).
- **Maintaining a per-provider transport layer.** Renderers emit
  request bodies; the host's SDK does the network call. The renderer
  is a pure function over `CompiledPrompt`.
- **Provider auto-detection.** The caller picks the provider. We will
  not sniff and route — that's `cross_vendor_routing`'s job.

## Open verification work for P3

Before writing the adapters, the implementer should re-confirm against
the provider docs:

| Item | What to confirm |
|---|---|
| OpenAI cache floor | Still 1,024 tokens? |
| OpenAI `prompt_cache_key` | Still the documented mechanism for sticky routing? |
| Anthropic breakpoint count | Still capped at four? |
| Anthropic minimum cacheable prefix | Per-model floors current? |
| Gemini implicit vs explicit | Behaviour on the latest model the user runs |
| DeepSeek usage field names | Match `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` exactly |

Update this file when the answers change.
