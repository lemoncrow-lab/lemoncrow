# Agent OS Workflow

Use this default loop for coding work in Atelier:

1. **Context** - read the relevant source of truth first. Use `context` when the
   Atelier MCP surface is available.
2. **Plan** - keep the plan small, concrete, and grounded in the relevant files.
3. **Implement** - make the change and update directly related docs when the rule
   surface changes.
4. **Recover** - if the same approach fails twice, use `rescue` or change
   strategy. Do not retry a third time with the same hypothesis.
5. **Record** - record observable outcomes with `record` or the host alias that
   exposes the same capability.

## Budget guardrails

- Name the deliverable before editing.
- Summarize the smallest viable plan.
- Keep context narrow: current goal, relevant files, failing command or output,
  and known constraints.
- Restate working context in under 10 bullets before editing or after compaction.
- If more than 10 minutes pass without an edit, restate the expected deliverable.

## Documentation loop

Update live docs when you change:

- repository-wide rules
- architecture boundaries
- validation commands
- plan and decision workflows
- host instruction generation
