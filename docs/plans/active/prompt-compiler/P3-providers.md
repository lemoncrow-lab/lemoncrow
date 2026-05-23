# P3 — Provider adapters

> Depends on: P1.
> Unblocks: P4, P7, P8.

## Goal

Render a `CompiledPrompt` into provider-shaped request bodies so each
provider's prefix-caching mechanism actually fires.

## Files

```
src/atelier/core/capabilities/prompt_compilation/
    providers.py
    providers_openai.py
    providers_anthropic.py
    providers_gemini.py
    providers_deepseek.py
tests/core/capabilities/prompt_compilation/
    test_providers_openai.py
    test_providers_anthropic.py
    test_providers_gemini.py
    test_providers_deepseek.py
```

## Public surface

```python
class Provider(str, Enum):
    OPENAI    = "openai"
    ANTHROPIC = "anthropic"
    GEMINI    = "gemini"
    DEEPSEEK  = "deepseek"

@dataclass(frozen=True)
class RenderedPrompt:
    provider: Provider
    request_body: dict[str, Any]   # ready to pass to the provider SDK
    prompt_cache_key: str | None   # OpenAI; None for others
    cache_breakpoints: tuple[int, ...]  # block indices that received cache_control (Anthropic)
    cache_metadata: dict[str, Any]      # explicit cache id (Gemini), notes for other vendors

def render(compiled: CompiledPrompt, provider: Provider, **opts: Any) -> RenderedPrompt: ...
```

Each provider module exports a pure `render(compiled, **opts) -> RenderedPrompt`.

## OpenAI adapter (`providers_openai.py`)

- Emits the Responses API shape (`input=[...]`), one message per block,
  preserving compile order.
- Computes `prompt_cache_key`:

  ```
  prompt_cache_key = "atelier:" + sha256(
      b"|".join(b.version_hash.encode() for b in stable_prefix)
  ).hexdigest()[:32]
  ```

  User task, diff, and tool output are intentionally **not** in the key.
- Refuses to emit `prompt_cache_key` if `stable_prefix_tokens < 1024`
  (logs a warning; the request still goes through, just without the
  key).
- Exposes `parse_usage(response) -> CacheUsage` returning
  `(cached_input_tokens, total_input_tokens)` from the SDK response.

## Anthropic adapter (`providers_anthropic.py`)

- Emits the Messages API shape (`system=[...]`, `messages=[...]`).
- Places `cache_control: {"type": "ephemeral"}` on the **last** block
  of the stable prefix (the last `branch` block).
- Optional `extra_breakpoints: list[BlockKind]`. For each kind, places
  an additional breakpoint at the last block of that kind. Caps at four
  breakpoints total (the documented Anthropic limit).
- Tool schemas go in the `tools=[...]` array; system blocks go in
  `system=[...]`; user task + diff + tool results become user messages.
  All other stable blocks (repo summary, ReasonBlocks, file summaries)
  become a single concatenated `system` block at the end of `system=`
  so the breakpoint can land cleanly on it.
- `parse_usage(response)` returns `(cache_creation_input_tokens,
  cache_read_input_tokens, input_tokens)`.

## Gemini adapter (`providers_gemini.py`)

- Default rendering: a single `contents=[...]` array, stable prefix
  first.
- Implicit caching mode (default): no extra metadata; rely on prefix
  matching.
- Explicit caching mode (opt-in via `opts["explicit_cache"]=True`):
  returns `cache_metadata={"create_cached_content": {...}}` describing
  the cache object to create out-of-band. The adapter does **not** call
  Gemini; the host does. We give them the payload.
- `parse_usage(response)` returns `(cached_content_token_count,
  total_token_count)`.

## DeepSeek adapter (`providers_deepseek.py`)

- Emits the Chat Completions shape (`messages=[...]`).
- Nothing to set on the request — DeepSeek auto-detects prefixes.
- `parse_usage(response)` returns `(prompt_cache_hit_tokens,
  prompt_cache_miss_tokens)`.

## Linter integration

Each renderer ships a small set of provider-specific lint rules,
registered with P2's plug-in API:

- `providers.openai.prefix-too-small` (WARN if `<1024`).
- `providers.anthropic.breakpoint-on-volatile` (ERROR if a breakpoint
  somehow lands on a non-stable block).
- `providers.gemini.explicit-cache-no-ttl` (WARN if explicit mode
  selected without a TTL hint).
- `providers.deepseek.no-changes-needed` (INFO).

These run from `linter.py` only when a provider context is supplied.

## Tests

- `test_providers_openai.py::test_cache_key_excludes_user_task`.
- `test_providers_openai.py::test_cache_key_stable_across_runs`.
- `test_providers_openai.py::test_no_key_below_1024_tokens`.
- `test_providers_anthropic.py::test_breakpoint_lands_on_last_branch_block`.
- `test_providers_anthropic.py::test_extra_breakpoints_respected_within_limit`.
- `test_providers_gemini.py::test_implicit_default`.
- `test_providers_gemini.py::test_explicit_cache_payload_shape`.
- `test_providers_deepseek.py::test_no_special_fields_on_request`.
- `test_providers_deepseek.py::test_parse_usage_extracts_hit_miss`.

All renderers are pure: same input → same output. Test by hashing
`json.dumps(request_body, sort_keys=True)` against a golden file.

## Acceptance

- `render(compiled, Provider.OPENAI)` returns a body that round-trips
  through the official `openai` SDK's request validation (do not call
  the network; use the SDK's `_construct_request` path or equivalent).
- Same for Anthropic, Gemini, DeepSeek SDKs (each gated behind an
  optional test dependency).
- `make lint && make typecheck` pass.

## Out of scope

- Calling the providers. We emit bodies; the host calls.
- Streaming. Streaming responses go through the host's SDK; the adapter
  only renders requests.
- A provider-agnostic LLM client. Out of scope forever — see CLAUDE.md
  boundary "no remote LLM calls in hot path".
