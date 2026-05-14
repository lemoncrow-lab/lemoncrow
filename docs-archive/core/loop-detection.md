# Loop Detection

Capability path:
- `src/atelier/core/capabilities/loop_detection/`

## Purpose

Loop detection finds repeated failures and dead-end behavior early.

## Behavior

- repeated command failure detection
- repeated tool-call detection
- second-guessing pattern detection
- dead-end trajectory detection
- blocker extraction from ledger history

## Runtime API

- `AtelierRuntimeCore.summarize_memory(...)`
- MCP: `trace`
