# atelier/integrations/copilot

Copilot host integration artifacts for VS Code.

- `install.sh` and `verify.sh` delegate to `scripts/install_copilot.sh` and `scripts/verify_copilot.sh`.
- `mcp.atelier.example.json` provides MCP server wiring examples.
- `COPILOT_INSTRUCTIONS.atelier.md` contains append-only instruction fragment.
- `chatmodes/atelier.chatmode.md` is installed to workspace `.github/chatmodes/` when `--workspace DIR` is used.
- `tasks.json` provides Atelier task presets merged into VS Code user tasks globally or workspace `.vscode/tasks.json` with `--workspace DIR`.

Keep docs, installer behavior, and verification checks aligned when changing any artifact here.

## V2 tool posture

Smart-tool cache posture: `read` and `search` are
default-on Atelier augmentations for repeated context reads/searches. Do not
remove host-native file reads, VS Code search, shell `rg`, or `grep`; those
remain the raw-access fallback. Use `ATELIER_CACHE_DISABLED=1` to disable the
Atelier cache.

Additional V2 Atelier augmentations available via MCP:

- **Memory**: `memory`, `memory`, `memory`, `memory`, `memory`
- **Context savings**: `search`, `edit`, `atelier sql inspect`, `compact`
- **Lesson pipeline**: `atelier lesson inbox`, `atelier lesson decide`

All V2 tools are Atelier augmentations. Copilot native tools (file reads, search, edit) remain the defaults.

## Trace confidence

- **Primary:** `mcp_live` — Atelier MCP tool calls and VS Code task outputs are captured.
  capture_sources: `["mcp"]`.
- **Fallback:** `manual` — agent calls `trace` with observable facts only.
- **Missing surfaces in primary mode:** `native_chat_edits`, `file_edits`.
- `full_live` and `hook_enforced` are not available for Copilot; hard blocking of
  model/tool calls and `provider_enforced` are future-only and disabled.

When calling `trace` from a Copilot session, include:

```json
"trace_confidence": "mcp_live",
"capture_sources": ["mcp"],
"missing_surfaces": ["native_chat_edits", "file_edits"]
```
