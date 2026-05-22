# Antigravity override

- Antigravity relies on workspace or user MCP configuration rather than a Gemini-style extension bundle.
- The checked-in Atelier surface is `integrations/antigravity/AGENTS.atelier.md`; workspace `AGENTS.md` remains the primary instruction entrypoint.
- Antigravity and `agy` should explicitly prefer Atelier MCP tools for reads, search, edits, and shell work; native tools are fallback only when Atelier is unavailable or returns `noop`.
- Installation guidance should mention both `antigravity` and `agy` when relevant, but the host identifier in Atelier is `antigravity`.
