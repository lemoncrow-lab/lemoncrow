# Claude Code Integration (Legacy Note)

This page predates the packaged Claude plugin workflow. Use
[hosts/claude-code-install.md](hosts/claude-code-install.md) for the current
installation and verification path.

## Current State

- The packaged Claude plugin and MCP install are the canonical integration surface.
- Active context/retrieval flows require `ATELIER_DEV_MODE=1`.
- `trace` is the stable observable recording surface; older `record-trace` shell
  examples should be read as `atelier trace record` if you are wiring raw CLI
  hooks by hand.
- Historical `check-plan`, `lint`, and similar examples on older pages do not
  match the current public CLI.

If you need a hand-written MCP entry instead of the packaged installer, mirror the
current `atelier-mcp` setup from [hosts/claude-code-install.md](hosts/claude-code-install.md).
