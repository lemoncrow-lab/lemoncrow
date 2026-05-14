# Core Capabilities

## Purpose

Runtime-managed capability layer for compounding reasoning behavior.

## Entry Points

- `__init__.py` - capability exports
- `context_reuse/` - reusable procedural retrieval and ranking
- `semantic_file_memory/` - local semantic summaries and AST symbol maps
- `loop_detection/` - repeated-failure and trajectory loop detection
- `tool_supervision/` - redundancy tracking and tool efficiency metrics
- `context_compression/` - ledger context compaction for next-step prompts
- `failure_analysis/` - failure clustering, root-cause hypotheses, and fix suggestions

## Key Contracts

- Capabilities are local-only and host-neutral.
- Capability logic is called by `AtelierRuntimeCore`, not directly from host adapters.
- Capability state persists under `<ATELIER_ROOT>/`.

## Where to look next

- `src/atelier/core/runtime/engine.py`
- `docs/core/capabilities.md`
