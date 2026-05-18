# Host Capability Matrix

This matrix is the durable contract for what Atelier expects from each supported
host. Use it when deciding whether a workflow should rely on native host
surfaces, Atelier's MCP wrapper, or a shell fallback.

Trace metadata fields used across this matrix:

- `trace_confidence`
- `capture_sources`
- `missing_surfaces`

Confidence legend: `full_live`, `mcp_live`, `wrapper_live`, `imported`, `manual`.

## Capability Matrix

| Host | Native surfaces Atelier uses | MCP | Hooks / events | Wrapper | Routing enforcement | Trace confidence | Unsupported controls | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | `AGENTS.md`, plugin agent prompt, CLI session exports | Yes, via `atelier-mcp --host claude` | Plugin hooks plus importable session artifacts | Shell install script plus generated host prompt | Medium: prompt-level guidance plus MCP mode controls | `full_live` when hooks + MCP are both present; otherwise `mcp_live`, `imported`, or `manual` | No native IDE task wiring beyond plugin surfaces | Use shell tasks and imported traces if live capture is unavailable |
| Codex CLI | `AGENTS.md`, session imports, generated host instructions | Yes, via `atelier-mcp --host codex` | No durable native hook bus | Shell installer and generated instructions | Medium: MCP/tool-mode guidance, not hard host enforcement | `mcp_live`, `wrapper_live`, `imported`, or `manual` | No first-class hook/event API and no guaranteed editor task bridge | Fall back to `atelier` CLI plus imported session history |
| Copilot | MCP config, Copilot instructions, chat mode, VS Code tasks | Yes, via VS Code MCP config | Task runner events only; no deep per-tool hook callbacks | VS Code task + instruction wrapper | Medium-high inside chat mode; shell tasks remain advisory | `mcp_live`, `wrapper_live`, or `manual` | No direct MCP invocation from `tasks.json`; limited background lifecycle visibility | Use `atelier` CLI tasks, worktree bootstrap, and runtime evidence capture |
| opencode | Generated agent markdown, session imports, local DB ingestion | Yes, via `atelier-mcp --host opencode` | No stable hook/event surface | Shell install script and generated agent file | Medium: prompt + MCP constraints | `mcp_live`, `imported`, or `manual` | No guaranteed host-side policy hooks or editor task plumbing | Fall back to imported traces and direct `atelier` CLI commands |
| Gemini CLI | `GEMINI.md`, generated extension prompt, session imports | Yes, via `atelier-mcp --host gemini` | Minimal; depends on imported chat artifacts | Shell installer and generated Gemini instructions | Medium: prompt contract plus MCP mode controls | `mcp_live`, `imported`, or `manual` | No rich hook/event contract and no native VS Code task integration | Use shell verification loops and trace recording from Atelier directly |
