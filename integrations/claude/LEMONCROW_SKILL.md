# LemonCrow Skill — Agent Reasoning Runtime

Use this skill to make your coding more reliable by leveraging the LemonCrow reasoning runtime.

## Activation

This skill activates automatically when you receive coding tasks. You can also invoke it explicitly with `/lemoncrow`.

## What It Does

LemonCrow provides:

- **Playbooks**: Proven procedures for specific domains (for example `swe.general`)
- **Rubrics**: Verification gates to ensure you didn't miss critical steps
- **Rescue procedures**: What to do when you hit repeated failures

## Commands

| Command             | What it does                      |
| ------------------- | --------------------------------- |
| `/lemoncrow:status`   | Show current run state            |
| `/lemoncrow:context`  | Show loaded Playbooks + rubric |
| `/lemoncrow:settings` | Show LemonCrow configuration        |

## MCP Tools

You have these tools via the `lc` MCP server:

```python
# Before a task - get relevant Playbook context
context(task="...", domain="...", tools=[...])

# On repeated failure - get rescue procedure
rescue(task="...", error="...", domain="...")

# After task - record the outcome
record(task="...", status="success|failed", ...)

# Before declaring done - verify against the domain's rubric
verify(rubric_id="<rubric id>", checks={...})
```

### V2 Memory tools [LemonCrow augmentation]

```python
# Store/retrieve named memory block
memory(agent_id="lemoncrow:code", label="last_gid", value="gid://shopify/Product/12345")
memory(agent_id="lemoncrow:code", label="last_gid")

# Archival memory — persist and recall
memory(agent_id="lemoncrow:code", text="...", source="run_123")
memory(agent_id="lemoncrow:code", query="Shopify GID pattern", top_k=5)

# Compact sleeptime memory to reduce context window
memory(session_id="run_123")
```

### V2 Context-savings tools [LemonCrow augmentation]

```python
# Combined token-saving search + read (host-native tools remain the raw-access fallback)
search(query="publish_product function", path="src/")

# Deterministic batch edits (optional — host MultiEdit remains default)
edit(edits=[{"path": "src/foo.py", "old": "...", "new": "..."}])

# Read-only SQL inspection
sql(connection_alias="default", sql="SELECT * FROM products LIMIT 5")

# Code index operations
code(op="search", repo_root=".", query="publish_product")

# Advise before host-native /compact — get preserve/reinject hints
compact(session_id="run_123")
```

## Domains

| Domain        | What it covers                                                   |
| ------------- | ---------------------------------------------------------------- |
| `swe.general` | Plan quality and failure recovery (codegen, debugging, refactor) |
| `coding`      | General coding (failure loops, etc.)                             |

## Example Usage

```
You: Fix the failing date-parsing test in utils/dates.py
→ Call context with domain=swe.general
→ Call verify after the fix with checks
→ Call record when done
```

## Verification

For critical tasks, run the rubric gate for the task's domain with `verify`.

If any `block_if_missing` check fails, stop and fix before proceeding.
