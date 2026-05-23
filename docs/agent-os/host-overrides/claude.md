# Claude override

- Claude plugin agent files can carry frontmatter for tools and display metadata.
- Claude entrypoints should prefer Atelier MCP tools for reads, search, edits, and shell work whenever those tools are exposed in the agent frontmatter.
- Claude-native file tools remain the raw-access fallback only when Atelier tools are unavailable or return `noop`.
- The main coding persona lives in `integrations/claude/plugin/agents/code.md`.
- **Subagent spawning**: when `route(op=spawn)` returns `handled=false`, call `Agent(agent_type="general-purpose", model=<spawn_directive.model>, prompt=<spawn_directive.prompt>)` immediately. Pass `run_in_background=True` for parallel waves. Omit `model=` entirely when it is `"inherit"`.
