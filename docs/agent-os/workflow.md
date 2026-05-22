# Agent OS Workflow

Use this default loop for coding work in Atelier:

1. **Context** - read the relevant source of truth first. Use `context` when the
   Atelier MCP surface is available.
2. **Plan** - keep the plan small, concrete, and grounded in the relevant files.
3. **Implement** - make the change with Atelier MCP tools for file I/O, search,
   edits, and shell work whenever they are available. Native host tools are
   fallback only when Atelier returns `noop`, is hidden, or is unavailable.
   Update directly related docs when the rule surface changes.
4. **Recover** - if the same approach fails twice, use `rescue` or change
   strategy. Do not retry a third time.
5. **Verify** - before concluding, apply the appropriate rubric with `verify`.
   Use `rubric_code_change` for coding work, `rubric_code_review` for reviews,
   `rubric_verification_ladder` to confirm a change is wired and substantive.
   See [review-rubric.md](review-rubric.md) for the full adversarial discipline.
6. **Record** - record observable outcomes with `record`. Include decisions,
   lessons, patterns, or surprises in the `learnings` parameter so they persist
   across sessions. See [learnings-flow.md](learnings-flow.md) for the protocol.

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
