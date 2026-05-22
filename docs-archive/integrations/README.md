# Atelier Integrations

Atelier is the reasoning runtime layer that sits between agent hosts and their environments.
It is not the IDE, not the agent, and not the memory system.

## Supported Hosts

| Host            | Install path          | Interface | Guide                                                     |
| --------------- | --------------------- | --------- | --------------------------------------------------------- |
| **Claude Code** | MCP + skills + agents | MCP stdio | [claude-code-install.md](../hosts/claude-code-install.md) |
| **Codex CLI**   | MCP + AGENTS.md       | MCP stdio | [codex-install.md](../hosts/codex-install.md)             |
| **Copilot**     | MCP + instructions    | MCP stdio | [copilot-install.md](../hosts/copilot-install.md)         |
| \*_opencode_    | `opencode.json`       | MCP stdio | [opencode-install.md](../hosts/opencode-install.md)       |
| **Gemini CLI**  | settings + MCP        | MCP stdio | [gemini-cli-install.md](../hosts/gemini-cli-install.md)   |

## Memory Systems

| System          | Module                                                     | Notes                                                                                                                                                                                                        |
| --------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --- |
| SQLite (native) | `src/atelier/infra/storage/sqlite_memory_store.py`         | Default. Local file, zero-server, fastest. WAL mode for concurrency.                                                                                                                                         |
| OpenMemory      | `src/atelier/gateway/integrations/openmemory.py`           | SQLite-backed bridge for trace-to-context pointers. Always-active when `ATELIER_MEMORY_BACKEND=openmemory`.                                                                                                  |
| Letta           | `src/atelier/infra/memory_bridges/letta_adapter.py`        | Client-server memory via `letta-client` SDK. Requires PostgreSQL + `letta server` (systemd unit: `atelier-letta.service`). Supports pgvector embeddings, agent-scoped memory, auto-compaction via sleeptime. |     |
| Generic vector  | `src/atelier/integrations/memory/generic_vector_memory.py` | OpenAI-compatible embedding endpoint.                                                                                                                                                                        |

Memory is facts. Atelier handles procedural reasoning. They complement, not duplicate, each other.

The Letta server runs as a **systemd user service** on Linux (`systemctl --user enable --now atelier-letta.service`), exposed on `http://127.0.0.1:8283`. Configure via `ATELIER_LETTA_URL` and `ATELIER_LETTA_API_KEY`. Install the client dependency with `pip install 'letta-client>=1.7.12'` or `atelier[memory]`.

## Safe Modes

All host integrations support:

| Mode      | Behaviour                                   |
| --------- | ------------------------------------------- |
| `shadow`  | Observe and record; never block             |
| `suggest` | Return warnings and rescue guidance         |
| `enforce` | Block plans that fail rubric gates (exit 2) |

Default for all supported hosts: `suggest`.
