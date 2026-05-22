# Copilot override

- Copilot sessions should use `.github/copilot-instructions.md` as the workspace entrypoint.
- Workspace chat mode lives at `.github/chatmodes/atelier.chatmode.md`.
- Copilot entrypoints must tell the agent to use Atelier MCP tools first for reads, search, edits, and shell work; VS Code native tools are fallback only when Atelier is unavailable or returns `noop`.
- VS Code tasks are the quickest way to expose repeatable preflight, worktree, and evidence loops.
