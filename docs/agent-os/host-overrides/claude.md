# Claude override

- Claude plugin agent files can carry frontmatter for tools and display metadata.
- Claude entrypoints should prefer Atelier MCP tools for reads, search, edits, and shell work whenever those tools are exposed in the agent frontmatter.
- Claude-native file tools remain the raw-access fallback only when Atelier tools are unavailable or return `noop`.
- The main coding persona lives in `integrations/claude/plugin/agents/code.md`.
