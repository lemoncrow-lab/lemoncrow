# Atelier Task Workflow (reference)

This document is the canonical task loop every Codex coding session
must follow when the Atelier MCP server is available.

## When this applies

Any coding task — bug fix, refactor, feature, migration, schema change —
in this workspace, especially anything touching:

- `shopify/**`, `pdp/**`, `catalog/**`, `tracker/**`, `publish/**`,
  `schema/**`
- backend services or alembic migrations
- domain code labeled `beseam.shopify.publish`, `beseam.pdp.schema`,
  `beseam.catalog.fix`, `beseam.tracker.classification`

## Loop

### 1. Retrieve task context

```json
task({
  "task": "Fix Shopify publish validation",
  "domain": "beseam.shopify.publish",
  "files": ["backend/src/modules/shopify/publish.py"],
  "errors": ["Handle 'foo-bar' not found"]
})
```

Read every returned ReasonBlock. Note their `dead_ends`.

### 2. Draft a plan

3–8 imperative steps. Reference the blocks where relevant.

### 3. Implement

Smallest diff that satisfies the validated plan.

### 4. Rescue repeated failures

Trigger: same command/test/tool fails twice with same error signature.

```json
rescue({
  "task": "Fix Shopify publish validation",
  "error": "AssertionError: metafield not updated",
  "files": ["..."],
  "domain": "beseam.shopify.publish",
  "recent_actions": [
    "ran pytest tests/test_publish.py",
    "edited publish.py",
    "ran pytest again"
  ]
})
```

### 5. Rubric gate (high-risk only)

```json
verify({
  "rubric_id": "rubric_shopify_publish",
  "checks": {
    "product_identity_uses_gid": true,
    "pre_publish_snapshot_exists": true,
    "write_result_checked": true,
    "post_publish_refetch_done": true,
    "post_publish_audit_passed": true,
    "rollback_available": true,
    "localized_url_test_passed": null,
    "changed_handle_test_passed": null
  }
})
```

`status == "blocked"` → fix the failing check before declaring success.

### 6. Record trace

```json
trace({
  "agent": "codex",
  "domain": "beseam.shopify.publish",
  "task": "Fix Shopify publish validation",
  "status": "success",
  "files_touched": ["backend/src/modules/shopify/publish.py"],
  "tools_called": [
    {"name": "task", "args_hash": "", "count": 1},
    {"name": "edit", "args_hash": "", "count": 3}
  ],
  "commands_run": ["pytest tests/test_publish.py"],
  "errors_seen": [],
  "diff_summary": "Switched product identity from handle to GID and added post-publish refetch.",
  "output_summary": "All 12 publish tests pass.",
  "validation_results": [
    {"name": "pytest", "passed": true, "detail": "12/12"}
  ]
})
```

## Hard rules

1. Never retry a failing command a third time without
   `rescue`.
2. Never declare success on a high-risk domain without
   `verify`.
3. Never record secrets, tokens, API keys, customer PII, or hidden
   chain-of-thought.
4. Never invent plan steps that contradict matched ReasonBlocks.
