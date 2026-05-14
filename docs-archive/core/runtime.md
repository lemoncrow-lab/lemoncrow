# Core Runtime

Atelier uses one runtime orchestrator: `AtelierRuntimeCore`.

Location:
- `src/atelier/core/runtime/engine.py`

## Responsibility

The runtime coordinates:
- capabilities
- rubrics
- traces
- evals
- environments
- storage

All capabilities execute automatically from this runtime. Host-specific adapters call one runtime surface and do not implement capability logic directly.

## Why this matters

This simplifies architecture and improves reliability:
- fewer call paths
- reduced duplicate logic
- shared cache/supervision metrics
- consistent behavior across CLI, MCP, SDK, and service
