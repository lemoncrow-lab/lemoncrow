# Prompt Compilation Capability — Agent README

> This module is the P0 foundation for the Atelier Prompt Compiler.
> Read `docs/plans/active/prompt-compiler/index.md` before editing any file here.

## What this module ships (P0)

- `Stability` — total order: STATIC < SESSION < BRANCH < TURN < VOLATILE
- `BlockKind` — 10 typed block categories (tool schemas, system, repo summaries, etc.)
- `DEFAULT_STABILITY` — canonical stability for each `BlockKind`
- `PromptBlock` — frozen dataclass with deterministic `version_hash` + `token_estimate`
- `tokens.estimate_tokens()` — tiktoken (cl100k_base) with char/4 fallback

## Invariants you must not break

1. `PromptBlock` is **immutable** (`frozen=True`) — never add mutable state
2. `version_hash` is sha256 of `content.encode("utf-8")` — identical across processes
3. `cacheable` is **forced False** for TURN and VOLATILE blocks — no caller override
4. Stability override requires `stability_override_reason` — always document why

## What is NOT here (deferred to later phases)

- Sorting / compilation → P1 (`compiler.py`)
- Cache-safety linting → P2 (`linter.py`)
- Provider adapters → P3 (`providers_*.py`)
- Trace row model → P5 (`trace.py`)
- Session inspector → P6 (`session_importers/`)
- MCP tool handler → P7 (extend `mcp_server.py`)
- Python SDK surface → P8 (`atelier/prompt_compiler/`)

## Dependency on existing modules

| Module | Usage |
|--------|-------|
| `core/capabilities/repo_map/budget.py` | `count_tokens` pattern (tiktoken cl100k_base) |
| `core/capabilities/pricing.py` | USD savings math (used in P5 trace, not P0) |
| `core/capabilities/telemetry/` | Trace substrate (used in P5, not P0) |
