# LemonCrow Task Workflow (reference)

This document is the canonical task loop every Codex coding session
must follow when the LemonCrow MCP server is available.

## When this applies

Any coding task — bug fix, refactor, feature, migration, schema change —
in this workspace, especially anything touching:

- `publish/**`, `schema/**`, `migrations/**`
- backend services or alembic migrations
- code in a domain that has a rubric (for example `swe.general`)

## Loop

### 1. Retrieve task context

```json
context({
  "task": "Fix publish validation",
  "domain": "swe.general",
  "files": ["backend/src/modules/publish/validate.py"],
  "errors": ["Handle 'foo-bar' not found"]
})
```

Read every returned Playbook. Note their `dead_ends`.

### 2. Draft a plan

3–8 imperative steps. Reference the blocks where relevant.

### 3. Implement

Smallest diff that satisfies the validated plan.

### 4. Rescue repeated failures

Trigger: same command/test/tool fails twice with same error signature.

```json
rescue({
  "task": "Fix publish validation",
  "error": "AssertionError: metafield not updated",
  "files": ["..."],
  "domain": "swe.general",
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
  "rubric_id": "rubric_change_safety",
  "checks": {
    "identity_uses_stable_id": true,
    "pre_change_snapshot_exists": true,
    "write_result_checked": true,
    "post_change_refetch_done": true,
    "post_change_audit_passed": true,
    "rollback_available": true,
    "edge_case_test_passed": null
  }
})
```

`status == "blocked"` → fix the failing check before declaring success.

### 6. Record trace

```json
record({
  "agent": "codex",
  "domain": "swe.general",
  "task": "Fix publish validation",
  "status": "success",
  "files_touched": ["backend/src/modules/publish/validate.py"],
  "tools_called": [
    {"name": "context", "args_hash": "", "count": 1},
    {"name": "edit", "args_hash": "", "count": 3}
  ],
  "commands_run": ["pytest tests/test_publish.py"],
  "errors_seen": [],
  "diff_summary": "Switched record identity from slug to stable id and added a post-publish refetch.",
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
4. Never invent plan steps that contradict matched Playbooks.
