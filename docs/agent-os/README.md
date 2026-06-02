# Agent OS

This directory is the live source of truth for how Atelier expects agents to
work in this repository.

Use these files instead of repeating the same operating rules in ad hoc prompts:

- [principles.md](principles.md) - the durable beliefs behind the repo design
- [workflow.md](workflow.md) - the default task loop for coding work
- [taste-invariants.md](taste-invariants.md) - rules that keep output legible
- [coding-guidelines.md](coding-guidelines.md) - the exact shared coding-guidance block used by generated agent surfaces
- [tool-substitution.md](tool-substitution.md) - mandatory native→Atelier tool mapping and project tooling conventions (uv, MCP, frontend, scripts)
- [validation-matrix.md](validation-matrix.md) - what to run for each change type
- [review-rubric.md](review-rubric.md) - adversarial review discipline and verification ladder
- [learnings-flow.md](learnings-flow.md) - how to extract and persist learnings across sessions
- [modes/](modes/) - canonical mode behavior for code, explore, review, repair, and research
- [host-overrides/](host-overrides/) - host-specific notes that sit on top of the shared rules

Generated entrypoints are derived from this tree:

- `AGENTS.md`
- `GEMINI.md`
- `.github/copilot-instructions.md`
- `.github/chatmodes/atelier.chatmode.md`
- `integrations/*` host instruction artifacts

Regenerate them after edits:

```bash
uv run python scripts/sync_agent_context.py
uv run python scripts/render_mode_surfaces.py
```
