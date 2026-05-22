# Unified Hosts Structure — Phase G

The `hosts/` module provides a unified integration point for all supported agent CLIs and IDE plugins.

## Structure

### `host_adapters/`
Host-specific session parsers and importers:
- `claude.py` — Claude Code session importer
- `codex.py` — Codex session importer
- `copilot.py` — Copilot session importer
- `opencode.py` — OpenCode session importer
- `_session_parser.py` — Shared session parsing logic

These adapters parse loss-preserving session data from each host and import it into Atelier's reasoning store.

### `configs/`
Host configuration templates (YAML):
- `claude.yaml` — Claude Code host config
- `codex.yaml` — Codex host config
- `copilot.yaml` — Copilot host config
- `antigravity.yaml` — Antigravity host config
- `opencode.yaml` — OpenCode host config

These define MCP server bindings, paths, and environment-specific settings per host.

### `registry.py`
Central host registration and discovery. Tracks:
- Host fingerprints and IDs
- Installed Atelier packs
- Available MCP tools
- Last seen timestamps

### `models.py`
Pydantic models for host registration, fingerprinting, and status tracking.

## Usage

**Import host adapters:**
```python
from atelier.hosts.host_adapters import ClaudeImporter, CodexImporter
```

**Load host registry:**
```python
from atelier.hosts.registry import HostRegistry
registry = HostRegistry()
```

## Backward Compatibility

The old `atelier.integrations.claude`, `.codex`, `.copilot`, `.opencode` paths are deprecated. Update imports to use:
```python
from atelier.hosts.host_adapters.claude import ClaudeImporter
```

## Related

- `atelier.memory_bridges` — Memory interoperability adapters (moved from integrations)
- `atelier.integrations` — Optional third-party integrations (ledger_reconstructor, openmemory only)
