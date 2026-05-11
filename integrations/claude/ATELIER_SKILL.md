# Atelier Skill — Agent Reasoning Runtime

Use this skill to make your coding more reliable by leveraging the Atelier reasoning runtime.

## Activation

This skill activates automatically when you receive coding tasks. You can also invoke it explicitly with `/atelier`.

## What It Does

Atelier provides:

- **ReasonBlocks**: Proven procedures for specific domains (Shopify, PDP, tracker, etc.)
- **Rubrics**: Verification gates to ensure you didn't miss critical steps
- **Rescue procedures**: What to do when you hit repeated failures

## Commands

| Command             | What it does                      |
| ------------------- | --------------------------------- |
| `/atelier:status`   | Show current run state            |
| `/atelier:context`  | Show loaded ReasonBlocks + rubric |
| `/atelier:settings` | Show Atelier configuration        |

## MCP Tools

You have these tools via the `atelier` MCP server:

```python
# Before a task - get relevant ReasonBlock
reasoning(task="...", domain="...", tools=[...])

# Before executing a plan - check for dead ends
lint(task="...", domain="...", plan=[...])
# Returns: {"status": "passed" | "warn" | "blocked", "warnings": [...], "dead_ends": [...]}

# On repeated failure - get rescue procedure
rescue(task="...", error="...", domain="...")

# After task - record the outcome
trace(task="...", status="success|failed", ...)

# For Shopify publish - verify against rubric
verify(rubric_id="rubric_shopify_publish", checks={...})
```

### V2 Memory tools [Atelier augmentation]

```python
# Store/retrieve named memory block
memory(agent_id="atelier:code", label="last_gid", value="gid://shopify/Product/12345")
memory(agent_id="atelier:code", label="last_gid")

# Archival memory — persist and recall
memory(agent_id="atelier:code", text="...", source="run_123")
memory(agent_id="atelier:code", query="Shopify GID pattern", top_k=5)

# Compact sleeptime memory to reduce context window
memory(session_id="run_123")
```

### V2 Context-savings tools [Atelier augmentation]

```python
# Combined token-saving search + read (host-native tools remain the raw-access fallback)
search(query="publish_product function", path="src/")

# Deterministic batch edits (optional — host MultiEdit remains default)
edit(edits=[{"path": "src/foo.py", "old_string": "...", "new_string": "..."}])

# Read-only SQL inspection
atelier sql inspect(connection_alias="default", sql="SELECT * FROM products LIMIT 5")

# Advise before host-native /compact — get preserve/reinject hints
compact(session_id="run_123")
```

### V2 Lesson pipeline tools [Atelier augmentation]

```python
# Review pending lesson candidates
atelier lesson inbox(domain="beseam.shopify.publish", limit=10)

# Approve or reject a candidate (approved → ReasonBlock)
atelier lesson decide(lesson_id="les_001", decision="approve", reviewer="atelier:code", reason="...")
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
→ Call reasoning with domain=beseam.shopify.publish
→ Call lint before making the API call
→ Call verify after publish with checks
→ Call trace when done
```

## Verification

For critical tasks, always run the rubric gate:

- Shopify publish → `rubric_shopify_publish`
- PDP fix → `rubric_pdp_schema`
- Classification → `rubric_ai_referral_classification`

If any `block_if_missing` check fails, stop and fix before proceeding.
