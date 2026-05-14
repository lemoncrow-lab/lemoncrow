---
id: WP-V3.1-A
title: Threshold-triggered tool-output compaction (`compact`)
phase: V3.1
boundary: Atelier-core
owner_agent: atelier:code
depends_on: [WP-33, WP-34, WP-35, WP-36, WP-39, WP-47, WP-49, WP-50]
supersedes: []
status: done
---

# WP-V3.1-A — `compact`

## Why

Bloated tool outputs are the largest single token sink in long coding sessions: full file
reads, verbose `grep` results, multi-page shell outputs, dense SQL result sets. The host's
premium LLM uses 5 % of these and wastes context on the rest.

V3.1-A ships a **threshold-triggered**, **opt-in**, **three-method** compaction MCP tool
that the host can call (or wire into a `PostToolUse` hook) to compress big outputs before
they reach the main LLM, with explicit recovery hints so nothing is lost forever.

This packet absorbs three earlier candidates that are all special cases of the same idea:

- **Stale-output sweeper** (ReasonBlocks-style): becomes the deterministic "drop or stub
  rarely-cited outputs" path.
- **SR2 compaction rule DSL + recovery hints**: becomes the per-content-type deterministic
  truncation strategy.
- **Cline duplicate-content collapsing**: becomes the deterministic "same file twice in
  trajectory → replace older with pointer" strategy.
- **Ollama-summarization at threshold**: becomes the high-budget summary method, gated to
  outputs above a size threshold so latency cost is amortized.

## Files touched

### MCP tool

- **NEW:** `src/atelier/core/capabilities/tool_supervision/compact_output.py` — the
  capability:
  - `compact(content, content_type, budget_tokens) -> CompactResult` per signature below.
  - Three-method gate by size:
    - `< 500 tokens`: `passthrough` — return content unchanged. No work done.
    - `500–2000 tokens`: `deterministic_truncate` — content-type-specific deterministic
      compression (see § "Deterministic strategies").
    - `> 2000 tokens`: `ollama_summary` — call `internal_llm.ollama_client.summarize` with
      explicit `budget_tokens`. On `OllamaUnavailable`, downgrade to
      `deterministic_truncate` (so the tool always returns _something_).
  - `CompactResult` Pydantic model:
    ```python
    class CompactResult(BaseModel):
        compacted: str
        original_tokens: int
        compacted_tokens: int
        recovery_hint: str          # how the host can re-fetch full content
        method: Literal["passthrough", "deterministic_truncate", "ollama_summary"]
        content_type: str
    ```
- **NEW:** `src/atelier/core/capabilities/tool_supervision/compact_strategies/` — one file
  per content type:
  - `file.py`: outline-first (reuses V2 `python_ast`); collapse duplicate occurrences in
    trajectory if `trajectory_files` arg passed.
  - `grep.py`: cluster hits by file; keep first 3 hits per file + count of remainder.
  - `bash.py`: keep stderr fully; truncate stdout to first/last K lines + `… (N lines
elided) …`. Recovery hint includes the exact rerun command if known.
  - `tool_output.py`: schema-and-sample for JSON/structured outputs (per SR2's pattern);
    raw-text fallback otherwise.
- **EDIT:** `src/atelier/gateway/mcp_server.py` — register
  `compact(content, content_type, budget_tokens)` MCP tool.

### Claude Code PostToolUse hook

- **NEW:** `integrations/claude/plugin/hooks/post_tool_use_compact.py` — a Claude Code
  `PostToolUse` hook script:
  - Reads the just-finished tool's output from the hook's stdin payload.
  - If `len(output_tokens) > threshold`, calls `compact` via MCP and
    returns the compacted output to be substituted in conversation.
  - Otherwise passthrough.
  - Configurable threshold via `.atelier/config.toml [compact].threshold_tokens`.
- **EDIT:** `integrations/claude/plugin/settings.json.example` — wire the hook in. The
  user opts in by adding the hook to their own `settings.json`; we don't auto-install.
- **NEW:** `integrations/claude/plugin/hooks/README.md` — documents the hook, recovery hint
  pattern, opt-in instructions.

### Telemetry

- **EDIT:** `src/atelier/core/capabilities/telemetry/context_budget.py` — record
  `compact_method` and per-method `tokens_in` / `tokens_out` per call. Surface
  `atelier_tokens_saved_total&#123;lever="compact_tool_output", method=...&#125;`.

### Tests

- **NEW:** `tests/core/test_compact_passthrough.py` — small inputs return unchanged.
- **NEW:** `tests/core/test_compact_deterministic_strategies.py` — one test per content
  type; assert deterministic output for fixed inputs.
- **NEW:** `tests/core/test_compact_ollama_summary.py` — with mocked Ollama, large input
  → calls Ollama with budget; recovery hint present.
- **NEW:** `tests/core/test_compact_ollama_unavailable_falls_back.py` — Ollama raises;
  result method is `deterministic_truncate`, not crash.
- **NEW:** `tests/gateway/test_post_tool_use_hook.py` — hook script smoke test with mocked
  MCP transport.

## Deterministic strategies (cheat sheet)

| `content_type` | Strategy at 500-2000 tokens                                                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `file`         | AST outline (V2 capability); if same file appears earlier in `trajectory_files`, replace with `[file X already read at step N — see above]` |
| `grep`         | Group hits by file; keep first 3 per file + `... and 17 more in foo.py`                                                                     |
| `bash`         | Full stderr; first 50 + last 50 lines of stdout with `… (X lines elided) …`                                                                 |
| `tool_output`  | JSON schema + 1-2 representative items + `len(items)` summary                                                                               |
| `unknown`      | Truncate to first/last K characters with elision marker                                                                                     |

Recovery hint always includes: how to re-fetch the original (offset/limit args, full path,
rerun command, or `query` to repeat). Format consistent across strategies so an agent prompt
can reliably parse it.

## How to execute

1. **Build the deterministic strategies first.** Five small modules; each is < 100 LOC.
   Test them in isolation.
2. **Add the dispatch / threshold gate.** This is the main `compact()` function; trivial
   over the strategies.
3. **Add the Ollama path.** Reuses `internal_llm.ollama_client.summarize` from WP-36.
   Important: **always include the recovery hint in the Ollama prompt** so the summary
   doesn't hide essential structure.
4. **Register the MCP tool** with strict input validation (max content length, allowed
   content types).
5. **Build the Claude Code hook.** Smoke-test on a real Claude Code session with a long
   `Read` output; confirm the substitution lands and recovery hint appears.
6. **Wire telemetry.** The `lever="compact_tool_output"` line goes into the WP-50 honest
   benchmark — finally, a measured-not-claimed savings number.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/core/test_compact_passthrough.py \
                     tests/core/test_compact_deterministic_strategies.py \
                     tests/core/test_compact_ollama_summary.py \
                     tests/core/test_compact_ollama_unavailable_falls_back.py \
                     tests/gateway/test_post_tool_use_hook.py -v

# Manual smoke:
LOCAL=1 uv run python -c "
from atelier.core.capabilities.tool_supervision.compact_output import compact
r = compact(content=open('src/atelier/cli/__init__.py').read(),
            content_type='file', budget_tokens=400)
print(r.method, r.original_tokens, '->', r.compacted_tokens)
print('---'); print(r.compacted[:500]); print('---'); print('hint:', r.recovery_hint)
"

make verify
```

## Definition of done

- [ ] `compact` MCP tool registered; three methods route correctly by
      size threshold.
- [ ] Five deterministic strategies implemented and tested.
- [ ] Ollama-summary path goes through `internal_llm.ollama_client`; never violates
      boundary.
- [ ] Recovery hints present and parseable on every non-passthrough result.
- [ ] Claude Code PostToolUse hook script ships; opt-in via settings.json snippet.
- [ ] Telemetry surfaces per-method savings.
- [ ] `make verify` green.
- [ ] V3 INDEX (V3.1 section) updated. Trace recorded.
