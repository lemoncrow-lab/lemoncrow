# OpenCode override

- OpenCode agent files should keep frontmatter because the host reads mode metadata from it.
- The OpenCode surface should still point to the same live docs tree as the other hosts.
- OpenCode entrypoints must explicitly prefer Atelier MCP tools for reads, search, edits, and shell work; native OpenCode tools are fallback only when Atelier is unavailable or returns `noop`.
- Keep the shared workflow, tool-substitution rules, and fallback rules aligned with the rest of the generated entrypoints.
