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
   For code changes, always run formatting, linting, type checks, and relevant
   tests from [validation-matrix.md](validation-matrix.md) before concluding.
   See [review-rubric.md](review-rubric.md) for the full adversarial discipline.
6. **Record** - record observable outcomes with `record`. Include decisions,
   lessons, patterns, or surprises in the `learnings` parameter so they persist
   across sessions. See [learnings-flow.md](learnings-flow.md) for the protocol.

## Delegation (cheaper-model subagents)

For expensive or self-contained subtasks (write tests, refactor a module, generate docs,
run a long search), delegate to a cheaper model instead of doing it inline:

1. Call `route(op=decide, task=<description>, budget=cheap)` — get recommended model.
2. If `can_spawn=true`, call `route(op=spawn, prompt=<full task prompt>, model=<from step 1>)`.
3. Read the response:
   - `handled=true` → the MCP server already ran it; result is in `response`. Done.
   - `handled=false` → **call your native spawn tool immediately** with `spawn_directive`:
     - **Claude Code**: `Agent(agent_type="general-purpose", model=..., prompt=...)`
     - **Copilot CLI**: `task(agent_type="general-purpose", prompt=...)`
     - **Codex / OpenCode**: `Task(prompt=..., model=...)`
4. Never inline work you received `handled=false` for — delegate it.
5. The subagent bootstraps its own Atelier context automatically via the prompt prefix.

## Budget guardrails

- Name the deliverable before editing.
- Summarize the smallest viable plan.
- Keep context narrow: current goal, relevant files, failing command or output,
  and known constraints.
- Restate working context in under 10 bullets before editing or after compaction.
- If more than 10 minutes pass without an edit, restate the expected deliverable.

## Symbol-first navigation

When the symbol name is known, use `code` ops — not text search:

1. **Known symbol name** → `code op="symbol"` (name-first lookup). Never `code op="search"` or raw grep.
2. **"Find code that looks like X"** → `code op="pattern"` (ast-grep structural match).
3. **"Find X and everything that calls/uses it"** → `code op="symbol"` then `code op="usages"`.
4. **Refactors targeting a named symbol** → `edit kind="symbol"` or `code op="pattern" rewrite=...`. Not raw `edit` with line numbers.

Callers/callees: use `code op="callers"` / `code op="callees"` instead of reading the file and tracing manually.

Deleted or renamed symbols: use `code op="search" scope="deleted"` with optional `since=` and `touched_by=` filters.

External dependencies: use `code op="search" scope="external"`. Do not attempt `edit kind="symbol"` on external targets — the engine rejects these before any file read.

Multi-repo workspaces: add `repo=<name>` to `code op="search"` or `code op="symbol"` to scope results.

## Documentation loop

Update live docs when you change:

- repository-wide rules
- architecture boundaries
- validation commands
- plan and decision workflows
- host instruction generation
