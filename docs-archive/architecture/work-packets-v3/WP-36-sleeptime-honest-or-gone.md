---
id: WP-36
title: Real sleeptime summarizer (Ollama-default, Letta fallback) OR remove from savings story
phase: Z
boundary: Cleanup
owner_agent: atelier:code
depends_on: [WP-33]
supersedes: [WP-09]
status: done
---

# WP-36 — Sleeptime: real or gone

## Why

The 2026-05-04 audit found `core/capabilities/context_compression/sleeptime.py` is ~65 LOC of
template-based grouping:

```python
f"[&#123;n&#125; &#123;kind&#125;s] &#123;last_event.summary[:200]&#125;"
```

The doc admits "deterministic, template-based, no LLM call". This is a glorified `groupby`
followed by string truncation. It cannot compress meaning. Counting it as a context-savings
"lever" overstates what V2 actually does, and inflates the (already-fictional) headline
percentage.

V3 picks one of two paths and commits.

## Decision criterion (updated for V3 with Ollama in scope)

**Atelier may call a small local LLM (Ollama) for internal/background processing.** That
re-opens a "real summarizer" path that doesn't violate the boundary, because the call is
local, free, runs in the background, and is never on the user's hot path.

- **(A) Real summarizer with Ollama-default + Letta fallback** (preferred):
  - **Default sub-path A1:** if Ollama is available locally (detected via the `[smart]`
    extra and `ollama-python` import), call a small local model (e.g. `llama3.2:3b`) to
    summarize old ledger entries. Return the summary.
  - **Fallback sub-path A2:** if Ollama is not available **and** `[memory].backend = "letta"`,
    delegate to Letta's sleeptime endpoint via `letta-client`.
  - **Last-resort sub-path A3:** if neither Ollama nor Letta is configured, raise
    `SleeptimeUnavailable`. Caller decides what to do (the host typically logs and skips
    summarization).
- **(B) Remove the lever:** delete the template summarizer; do not list "sleeptime" as a
  context-savings lever in V3 docs/benchmarks.

**Pick (A).** The new default with Ollama is shippable for everyone with a local model server
running, which is becoming standard in coding-agent setups. Letta-delegated path remains for
users running self-hosted Letta. The only reason to pick (B) over (A) is if for some reason
neither `[smart]` extra nor Letta is part of the project's distribution story — at the time
of this packet's writing, that is not the case.

> **Note on default:** path (A) replaces the V2 template summarizer with a real summary.
> It does not promise "compression" without measurement — telemetry records `input_tokens`
> sent to Ollama and `output_tokens` returned, and the lever's reported saving is _net_
> (`input - output`).

## Files touched

### Path A (default)

- **EDIT:** `pyproject.toml` — add `ollama-python>=0.4.0` to a new optional extra `[smart]`.
- **NEW:** `src/atelier/infra/internal_llm/__init__.py` — namespace for internal-only LLM
  callers. **Atelier-wide rule (added in WP-36):** model-client imports outside this module
  and `infra/embeddings/` are a CI failure (the grep gate from § "Boundary enforcement"
  below).
- **NEW:** `src/atelier/infra/internal_llm/ollama_client.py` — thin wrapper over
  `ollama-python` with:
  - `summarize(text, *, model="llama3.2:3b", max_tokens=256) -> str`
  - `chat(messages, *, model, json_schema=None) -> str | dict` (used by V3.1 packets)
  - Lazy import of `ollama`; raises `OllamaUnavailable` if the extra isn't installed or the
    Ollama server isn't reachable on `OLLAMA_HOST`.
- **EDIT:** `src/atelier/core/capabilities/context_compression/sleeptime.py` — replace the
  template body:
  - Try Ollama via `internal_llm.ollama_client.summarize` (path A1).
  - On `OllamaUnavailable`, if `[memory].backend == "letta"`, delegate to Letta via
    `letta-client` (path A2).
  - Otherwise raise `SleeptimeUnavailable` (path A3).
- **EDIT:** `src/atelier/core/capabilities/telemetry/context_budget.py` — record
  `summarizer_input_tokens` and `summarizer_output_tokens`. Surface
  `atelier_tokens_saved_total&#123;lever="sleeptime"&#125;` as `input - output`. If sleeptime raised,
  do **not** record any saving for that turn.
- **NEW:** `tests/core/test_sleeptime_ollama_default.py` — with a mocked Ollama backend,
  asserts:
  - The summarizer is invoked with a small fixture ledger.
  - The returned summary is shorter than the input by at least N tokens.
  - The metric is recorded net.
- **NEW:** `tests/core/test_sleeptime_letta_fallback.py` — with Ollama unavailable, mocked
  Letta sidecar (`respx`), asserts the call is forwarded to Letta.
- **NEW:** `tests/core/test_sleeptime_unavailable.py` — neither Ollama nor Letta:
  asserts `SleeptimeUnavailable` is raised explicitly (no silent fallback to template).

### Path B (last resort, only if A is rejected)

- Same as the prior version of this packet — delete `sleeptime.py`, remove the lever from
  telemetry, drop callers, ship `tests/infra/test_no_sleeptime_lever.py`.

### Both paths

- **NEW:** `docs/internal/engineering/decisions/004-sleeptime-decision.md` — short ADR
  recording (A) chosen and the Ollama-default rationale.

## Boundary enforcement (added in WP-36, applies repo-wide)

**New CI grep gate:** `tests/infra/test_no_external_llm_clients.py`. Fails if any file under
`src/atelier/` imports any of:

- `anthropic`, `anthropic_bedrock`, `boto3.bedrock_runtime`
- `openai` (allowed only in `infra/embeddings/openai_embedder.py`)
- `litellm`, `cohere`, `mistralai`, `google.generativeai`, `replicate`

`ollama` is allowed **only** inside `src/atelier/infra/internal_llm/`.

This gate codifies the V3 boundary: no model client imports on the user's hot path; Ollama
allowed inside the dedicated internal-processing module only.

## How to execute

1. **Add the `[smart]` extra and `internal_llm` module first.** This is the foundation for
   WP-36, the WP-47 Reflexion step, and all four V3.1 packets that need Ollama.
2. **Implement the three sub-paths** in the new `sleeptime.py`. Sub-path resolution is
   compile-time configurable via `[memory].backend` and runtime-detectable via Ollama
   availability.
3. **Add the boundary CI gate** (`test_no_external_llm_clients.py`). Confirm it fails before
   any code change (because V2 may have residual imports) and passes after.
4. **Update telemetry** to record net savings; never inflate.
5. **Document via the ADR** — record both why path A was chosen and the architectural rule
   (`internal_llm` module is the only LLM-import path outside embeddings).

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

# Three sub-paths, all mocked:
LOCAL=1 uv run pytest tests/core/test_sleeptime_ollama_default.py \
                     tests/core/test_sleeptime_letta_fallback.py \
                     tests/core/test_sleeptime_unavailable.py -v

# Boundary gate:
LOCAL=1 uv run pytest tests/infra/test_no_external_llm_clients.py -v

# Manual smoke (requires a real local Ollama server):
# ollama serve & ollama pull llama3.2:3b
# LOCAL=1 uv run python -c "
# from atelier.core.capabilities.context_compression.sleeptime import summarize_ledger
# print(summarize_ledger([&#123;'kind':'tool','summary':'long output ...'&#125; for _ in range(50)]))
# "

make verify
```

## Definition of done

- [ ] `[smart]` extra added with `ollama-python` dependency.
- [ ] `src/atelier/infra/internal_llm/` module created with `ollama_client.py`.
- [ ] Sleeptime resolves via A1 → A2 → A3, no silent fall-back to V2 template.
- [ ] Telemetry records net savings; no inflation.
- [ ] Boundary CI gate (`test_no_external_llm_clients.py`) wired into `make verify` and
      passes.
- [ ] ADR `004-sleeptime-decision.md` filed.
- [ ] V3 plan reflects the outcome.
- [ ] `make verify` green.
- [ ] V3 INDEX status updated. Trace recorded.
