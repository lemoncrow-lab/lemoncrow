# Codex Integration (Legacy Note)

This page predates the packaged Codex plugin and wrapper workflow. Use
[hosts/codex-install.md](hosts/codex-install.md) for the current install path.

## Current State

- The maintained Codex integration is the packaged plugin plus the generated
  `atelier-codex` wrapper.
- Active context/retrieval operations require `ATELIER_DEV_MODE=1` for the MCP
  server or the wrapper environment.
- `trace` remains the stable observable recording surface.
- Older `lint`, `check-plan`, or early tool-reference examples on legacy pages do
  not match the current public CLI.

If you need raw MCP wiring instead of the packaged installer, use the current
`atelier-mcp` entrypoint and the wrapper notes from
[hosts/codex-install.md](hosts/codex-install.md).
