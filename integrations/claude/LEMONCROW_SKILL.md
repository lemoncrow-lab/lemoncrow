# LemonCrow Skill — Agent Reasoning Runtime

Use this skill to make your coding more reliable by leveraging the LemonCrow reasoning runtime.

## Activation

This skill activates automatically when you receive coding tasks. You can also invoke it explicitly with `/lemoncrow`.

## What It Does

LemonCrow provides:

- **Playbooks**: Proven procedures for specific domains (Shopify, PDP, tracker, etc.)
- **Rubrics**: Verification gates to ensure you didn't miss critical steps
- **Rescue procedures**: What to do when you hit repeated failures

## Commands

| Command             | What it does                      |
| ------------------- | --------------------------------- |
| `/lc:status`   | Show current run state            |
| `/lc:context`  | Show loaded Playbooks + rubric |
| `/lc:settings` | Show LemonCrow configuration        |

## MCP Tools

You have these tools via the `lc` MCP server:

```python
# Before a task - get relevant Playbook context
context(task="...", domain="...", tools=[...])

# On repeated failure - get rescue procedure
rescue(task="...", error="...", domain="...")

# After task - record the outcome
record(task="...", status="success|failed", ...)

# For Shopify publish - verify against rubric
verify(rubric_id="rubric_shopify_publish", checks={...})
```

### V2 Memory tools [LemonCrow augmentation]

```python
# Store/retrieve named memory block
memory(agent_id="lc:code", label="last_gid", value="gid://shopify/Product/12345")
memory(agent_id="lc:code", label="last_gid")

# Archival memory — persist and recall
memory(agent_id="lc:code", text="...", source="run_123")
memory(agent_id="lc:code", query="Shopify GID pattern", top_k=5)

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

### V2 Lesson pipeline tools [LemonCrow augmentation]

```python
# Review pending lesson candidates
lc lesson inbox(domain="beseam.shopify.publish", limit=10)

# Approve or reject a candidate (approved → Playbook)
lc lesson decide(lesson_id="les_001", decision="approve", reviewer="lc:code", reason="...")
```

## Domains

| Domain                          | What it covers                                 |
| ------------------------------- | ---------------------------------------------- |
| `beseam.shopify.publish`        | Shopify product publishing, identity, rollback |
| `beseam.pdp.schema`             | PDP validation, structured data authority      |
| `beseam.catalog.fix`            | Catalog → PDP sync, ingest correctness         |
| `beseam.tracker.classification` | AI referral vs organic classification          |
| `beseam.audit_service_change`   | Audit service data changes                     |
| `coding`                        | General coding (failure loops, etc.)           |

## Example Usage

```
You: Update the product title for handle "winter-coat"
→ Call context with domain=beseam.shopify.publish
→ Call verify after publish with checks
→ Call record when done
```

## Verification

For critical tasks, always run the rubric gate:

- Shopify publish → `rubric_shopify_publish`
- PDP fix → `rubric_pdp_schema`
- Classification → `rubric_ai_referral_classification`

If any `block_if_missing` check fails, stop and fix before proceeding.
