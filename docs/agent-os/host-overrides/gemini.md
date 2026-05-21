# Gemini override

- Gemini loads `GEMINI.md` or the bundled extension copy as project context.
- Gemini entrypoints must explicitly prefer Atelier MCP tools for reads, search, edits, and shell work; Gemini-native tools are fallback only when Atelier is unavailable or returns `noop`.
- Gemini instructions should stay short and focus on the shared workflow plus precise fallback wording.
- Slash-command guidance belongs in the extension-facing Gemini surface, not in every host file.
