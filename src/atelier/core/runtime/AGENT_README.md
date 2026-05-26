# Core Runtime

## Purpose

Single orchestrator for capabilities, storage, traces, environments, and runtime utilities.

## Entry Points

- `engine.py` - `AtelierRuntimeCore`
- `__init__.py` - runtime export

## Key Contracts

- One runtime surface is shared by CLI, MCP, SDK, and service wrappers.
- Runtime owns capability lifecycle and state access.
- Host adapters remain thin and defer operational logic to runtime.

## Where to look next

- `src/atelier/gateway/adapters/runtime.py`
- `src/atelier/gateway/adapters/mcp_server.py`
- `src/atelier/gateway/cli/app.py`
