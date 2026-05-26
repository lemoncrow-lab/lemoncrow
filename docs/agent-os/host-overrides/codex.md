# Codex override

- Codex loads `AGENTS.md` as the workspace entrypoint.
- Codex entrypoints must explicitly tell the agent to call Atelier context first on coding tasks and to use Atelier MCP tools first for reads, search, code intelligence, edits, and shell work; native Codex tools are fallback only when Atelier is unavailable or returns `noop`.
- Keep Codex instructions short and point to the same live docs tree as the other hosts.
- The Codex integration surface should stay aligned with the shared budget guardrails, tool-substitution rules, and fallback rules.
