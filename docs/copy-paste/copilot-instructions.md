# Copilot Instructions Block (Copy-Paste)

Use this block if you can't use the MCP server and need to manually inject Atelier's reasoning context into GitHub Copilot Chat or a `.github/copilot-instructions.md` file.

---

## Minimal Block (for `.github/copilot-instructions.md`)

```markdown
## Atelier Reasoning Runtime

Before starting any task in this repository, you MUST follow the Atelier reasoning loop:

1. **Check your plan** — Before editing files, consider whether your plan contains any known dead ends for this domain.

Known baseline dead ends (DO NOT USE):

- "Resolve target from URL slug alone" → resolve and record the canonical identifier first
- "Continue patching before validation" → run the cheapest focused validation after each edit

2. **Verify output** — After completing a task in `state.change` domain, all of the following must be true:

- `canonical_identifier_used: true`
- `pre_change_state_captured: true`
- `read_after_write_completed: true`
- `observed_state_matches_intent: true`

3. **Record what happened** — After each task, state what commands you ran, what errors you saw, and what the outcome was.

If MCP is configured (`atelier` server), use `lint`, `verify`, and `trace` instead of doing the above manually.
```

---

## Full MCP-Enabled Block

If Copilot Chat has MCP access (configured via `.vscode/mcp.json`), add this to your instructions:

```markdown
## Atelier Reasoning Runtime (MCP)

MCP server `atelier` is available. Use it on every task:

**Before executing:**

1. Call `reasoning` with the task + domain
2. Call `lint` with your proposed steps — if it returns `status: blocked`, revise your plan

**After executing:** 3. Call `verify` with the appropriate rubric and your results 4. Call `trace` with the execution summary

Available rubrics: `rubric_code_change`, `rubric_state_change_safety`, `rubric_debugging_task`, and others (use `search` to find domain-specific ones).
```

---

## Setup for `.vscode/mcp.json`

```json
&#123;
  "servers": &#123;
    "atelier": &#123;
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "$&#123;workspaceFolder&#125;/atelier", "atelier-mcp"],
      "env": &#123;
        "ATELIER_WORKSPACE_ROOT": "$&#123;workspaceFolder&#125;"
      &#125;
    &#125;
  &#125;
&#125;
```
