# Gateway Adapters

## Purpose

Host-facing integration surfaces for CLI, MCP, service, and embedded runtime adapters.

## Entry Points

- `cli.py` - primary `atelier` CLI surface
- `mcp_server.py` - MCP tool server and tool registry
- `runtime.py` - in-process runtime adapter used by product agents
- `http_api.py` - HTTP API adapter
- `remote_client.py` - service-mode remote client

## Key Contracts

- Adapters call one runtime orchestrator (`AtelierRuntimeCore`) for capability logic.
- Legacy command/tool surfaces remain available while new capability commands/tools are added.
- Host adapters are thin wrappers; policy and state logic live in core runtime/capabilities.
- Domain bundles are internal-only, loaded from built-in and local domain directories.

## Capability-driven tools and commands

- CLI: `capability list/status`, `memory summarize`, `read smart`, `edit smart`, `sql inspect`, `benchmark runtime`, `benchmark hosts`
- MCP: `context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `code`, `shell`

## MCP Tool Surface

| Tool | Namespace | Purpose |
|---|---|---|
| `context` | brain | Retrieves curated task context, memory, and ledger state |
| `route` | brain | Decides and verifies execution routes |
| `rescue` | brain | Suggests recovery for a failed step |
| `verify` | brain | Evaluates rubric checks and returns pass/blocked status |
| `trace` | capture | Records trace + realtime prompt/response/bash compaction + optional Langfuse emit |
| `memory` | memory | Dispatches block, archive, recall, and summary ops |
| `read` | context | Reads files with smart truncation |
| `search` | context | Searches files with chunk, full, and map modes |
| `edit` | edit | Applies supervised deterministic edits |
| `sql` | data | Runs bounded read-only SQL inspection |
| `compact` | infra | Returns compact ledger prompt block plus realtime context snapshot |
| `code` | code | Dispatches index, search, symbol, outline, context, and impact ops |
| `shell` | execution | Executes compact supervised shell commands |

Remote mode (`ATELIER_MCP_MODE=remote`) routes the core HTTP-backed tools through `remote_client.py`; `compact` is always local.

`rescue` now enriches responses with failure-cluster analysis (`analysis`) derived from historical failed traces.

## OpenMemory CLI Notes

- `atelier openmemory status` always shows local bridge tools.
- `ATELIER_OPENMEMORY_ENABLED=true` switches mode to local + remote sync.
- Disabled remote mode still keeps local persistence available.

## Where to look next

- `src/atelier/core/runtime/engine.py`
- `src/atelier/core/capabilities/`
- `docs/core/runtime.md`
- `docs/core/capabilities.md`
