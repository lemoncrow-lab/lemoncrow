# Agent OS

This directory is the live source of truth for how Atelier expects agents to
work in this repository.

Use these files instead of repeating the same operating rules in ad hoc prompts:

- [principles.md](principles.md) - the durable beliefs behind the repo design
- [workflow.md](workflow.md) - the default task loop for coding work
- [taste-invariants.md](taste-invariants.md) - rules that keep output legible
- [validation-matrix.md](validation-matrix.md) - what to run for each change type
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
```
