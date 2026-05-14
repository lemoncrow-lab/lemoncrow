# Semantic Memory

Capability path:
- `src/atelier/core/capabilities/semantic_file_memory/`

## Purpose

Semantic memory stores local file meaning to avoid repeated full reads.

## Behavior

- local-only cache under `.atelier/semantic_file_memory.json`
- captures summaries, symbol maps, export lists
- supports Python AST summaries
- enables semantic lookup over cached file meaning

## Runtime API

- `AtelierRuntimeCore.smart_read(...)`
- MCP: `read`, `search`
