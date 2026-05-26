# atelier/integrations/codex

Codex host integration artifacts.

- `install.sh` and `verify.sh` are thin wrappers around `scripts/install_codex.sh` and `scripts/verify_codex.sh`.
- `plugin/.mcp.json` defines the Codex MCP server entry.
- `AGENTS.atelier.md` is the source copied to `~/.codex/AGENTS.md` globally or `<workspace>/AGENTS.md` with `--workspace DIR`.
- `tasks/` contains reusable Codex task prompts for preflight and recovery workflows.
- `references/` stores host-specific notes and examples.

If install/verify behavior changes, update `scripts/install_codex.sh`, `scripts/verify_codex.sh`, and this file together.

## V2 tool posture

The following Atelier MCP tools are available and documented in `tasks/preflight.md`:

- **Memory** (Atelier augmentation): `memory`, `memory`, `memory`, `memory`, `memory`
- **Context savings** (Atelier augmentation): `search`, `edit`, `code`, `sql`, `compact`
- **Lesson pipeline** (Atelier augmentation): `atelier lesson inbox`, `atelier lesson decide`

All V2 tools are Atelier augmentations. Native Codex `Read`, shell `rg`/`grep`, and `MultiEdit` may still be exposed by the host, but Atelier policy treats them as fallback-only when the Atelier MCP equivalents are available.

## Trace confidence

- **Primary:** `mcp_live` + `wrapper_live` — Atelier MCP tool calls and wrapper task start/end are
  captured. capture_sources: `["mcp", "wrapper"]`.
- **Fallback:** `manual` — agent calls `record` with observable facts only.
- **Missing surfaces in primary mode:** `bash_outputs`, `file_edits`, `native_shell`.
- `full_live` is not available for Codex; `hook_enforced` parity with Claude Code plugin hooks is
  future-only and disabled.

When calling `record` from a Codex session, include:

```json
"trace_confidence": "mcp_live",
"capture_sources": ["mcp", "wrapper"],
"missing_surfaces": ["bash_outputs", "file_edits"]
```
