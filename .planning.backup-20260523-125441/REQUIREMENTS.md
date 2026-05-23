# Requirements: v1.1 Prompt Compiler

> Milestone: v1.1 — cache-safe context assembly for coding agents
> Created: 2026-05-23
> Source of truth: `docs/plans/active/prompt-compiler/`

## v1.1 Requirements

### Block Model (BLOC)

- [ ] **BLOC-01**: User's agent can represent a prompt block with stability level, kind, content, hash, and token estimate so cache-unsafe insertions are caught at type time
- [ ] **BLOC-02**: Developer can sort blocks by the total order STATIC < SESSION < BRANCH < TURN < VOLATILE so prompt order is deterministic without manual reasoning

### Compiler (COMP)

- [ ] **COMP-01**: Developer can call `compile_prompt(blocks)` and receive a `CompiledPrompt` with a deterministic cache-prefix boundary, prefix hash, and tail budget plan
- [ ] **COMP-02**: Developer can pass an optional `tail_budget_tokens` cap and have turn/volatile blocks knapsack-packed by the existing budget optimizer without touching the stable prefix

### Linter (LINT)

- [ ] **LINT-01**: Developer can call `lint(blocks)` and get actionable `LintFinding` diagnostics with severity when volatile blocks appear before stable ones, tool schemas are reordered across turns, timestamps or request IDs leak into the prefix, or the stable prefix is undersized
- [ ] **LINT-02**: Developer can run the linter before any compile call and receive a pass/warn/error verdict that can be enforced in CI

### Provider Adapters (PROV)

- [ ] **PROV-01**: Developer can render a `CompiledPrompt` into an OpenAI request body with the correct `prompt_cache_key` headers so OpenAI's prefix cache fires on repeated stable prefixes
- [ ] **PROV-02**: Developer can render a `CompiledPrompt` into an Anthropic request body with `cache_control: {"type": "ephemeral"}` markers at the prefix boundary
- [ ] **PROV-03**: Developer can render for Gemini (implicit) and DeepSeek (hit/miss parsing) so all four supported providers get a pure-function renderer over `CompiledPrompt`

### CLI Surface (CLI)

- [ ] **CLI-01**: Developer can run `atelier prompt compile <blocks.json>` with optional `--provider` and `--tail-budget-tokens` flags and receive a rendered prompt + cache metadata on stdout or to file
- [ ] **CLI-02**: Developer can run `atelier prompt lint <blocks.json>` with text or JSON output format and see which rules fire with actionable remediation
- [ ] **CLI-03**: Developer can run `atelier prompt inspect-session <PATH>` to replay a Claude Code or Codex JSONL session through the compiler and see a cache-breaker diagnosis report

### Trace & Telemetry (TRAC)

- [ ] **TRAC-01**: Every `compile()` call persists a `PromptCompilationTrace` row recording `stable_prefix_tokens`, `dynamic_tail_tokens`, `cache_lint_score`, `stable_prefix_hash`, and the list of cache-breakers found so savings are proven, not assumed
- [ ] **TRAC-02**: Atelier traces record estimated USD cache savings (`cache_read_tokens × cached_input_price`) so the economic story is visible in the existing scorecard

### Session Inspector (INSP)

- [ ] **INSP-01**: Developer can replay any existing Claude Code / Codex / OpenAI JSONL session through the compiler and receive a diagnosis listing exactly which blocks break the prefix cache and why, ordered by cost impact

### MCP Tool (MCP)

- [ ] **MCP-01**: Agent using the Atelier MCP server can invoke the compiler in one round-trip and receive a compiled prompt with cache metadata, without needing the Python SDK installed

### Python SDK (SDK)

- [ ] **SDK-01**: Developer can `from atelier.prompt_compiler import PromptCompilerCapability, PromptBlock, Stability` and call `compile()`, `lint()`, `render()`, and `attach_usage()` from any custom coding agent with no MCP dependency

## Future Requirements (deferred)

- JavaScript/TypeScript SDK for editor-side agents — out of scope for v1.1, revisit after stable Python surface
- `atelier-prompt-compiler` split PyPI package — decide after first external user
- Provider-specific tokenizers (beyond tiktoken) — revisit if traces show drift > 5%
- DEFR-01: Broader cross-language/runtime edges (JNI, Rust FFI, runtime traces)
- DEFR-02: Build-system dependency graphs (Bazel/Buck)
- M14–M18 code-intel milestones from v1.0 — v2 candidates

## Out of Scope

- Atelier calling OpenAI/Anthropic/Gemini/DeepSeek from the hot path — the compiler emits blocks + cache hints; the host owns the LLM call
- New prompt template DSL — blocks are typed text + metadata (no Jinja, no chains)
- Model routing — that lives in `cross_vendor_routing/` and `quality_router/`, unchanged
- Writing to ReasonBlocks or semantic memory — the compiler reads only, never writes
- Atelier Gateway product line (proxy + router + guardrails + loop detector) — separate product, not v1.1

## Traceability

| Requirement | Phase | Plan | Status |
|-------------|-------|------|--------|
| BLOC-01, BLOC-02 | Phase 8 | 08-01 | — |
| COMP-01, COMP-02 | Phase 9 | 09-01 | — |
| LINT-01, LINT-02 | Phase 10 | 10-01 | — |
| PROV-01, PROV-02, PROV-03 | Phase 11 | 11-01 | — |
| TRAC-01, TRAC-02 | Phase 12 | 12-01 | — |
| CLI-01, CLI-02, CLI-03 | Phase 13 | 13-01 | — |
| INSP-01 | Phase 14 | 14-01 | — |
| MCP-01 | Phase 15 | 15-01 | — |
| SDK-01 | Phase 16 | 16-01 | — |
