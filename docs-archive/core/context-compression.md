# Context Compression

Capability path:
- `src/atelier/core/capabilities/context_compression/`

## Purpose

Context compression keeps decision-critical information while removing stale history.

## Preserved fields

- latest errors
- active blockers
- validation requirements
- current hypothesis context

## Behavior

- compresses run ledger state
- produces compact prompt blocks
- returns runtime-safe summaries for the next step

## Runtime API

- `AtelierRuntimeCore.summarize_memory(...)`
- CLI: `atelier memory summarize`
- MCP: `compact`
