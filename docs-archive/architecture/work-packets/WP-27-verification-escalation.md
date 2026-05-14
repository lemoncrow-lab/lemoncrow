---
id: WP-27
title: Implement verification-gated escalation policy
phase: F
pillar: routing
owner_agent: atelier:code
depends_on: [WP-25, WP-26]
status: done
---

# WP-27 - Verification-Gated Escalation

## Why

Cheap calls only save money when they produce accepted patches. The verifier must convert tests,
lint, typecheck, rubric results, protected-file checks, and repeated-failure signals into route
outcomes and escalation triggers.

## Files touched

- **Create** `src/atelier/core/capabilities/quality_router/verifier.py`
- **Edit** `src/atelier/core/capabilities/quality_router/capability.py`
- **Edit** `src/atelier/gateway/adapters/mcp_server.py` — register `route`
- **Edit** `src/atelier/gateway/adapters/cli.py` — add `route verify` command
- **Create** `tests/core/test_routing_verifier.py`
- **Create** `tests/gateway/test_mcp_route_verify.py`

## How to execute

1. Import `VerificationEnvelope` from `src/atelier/core/foundation/routing_models.py`; do not
   redefine fields outside the data-model doc.
2. Capture validation command results, changed files, rubric gate status, and human/benchmark
   acceptance when present.
3. Implement escalation rules for failed tests, missing required verification, protected-file
   changes, unexpectedly large diffs, and repeated failure signatures.
4. Return compressed failure evidence suitable for a premium retry.
5. Do not run shell commands directly from the verifier; consume observed validation results only.
6. Add tests for pass, warn, fail, escalate outcomes, and MCP tool registration.

## Acceptance tests

```bash
uv run pytest tests/core/test_routing_verifier.py tests/gateway/test_mcp_route_verify.py -q
```

## Definition of done

- [ ] Verification outcomes are structured and serializable
- [ ] Escalation includes concise evidence, not full conversation history
- [ ] Verifier does not execute shell commands
- [ ] `route` is registered in MCP and mirrored by CLI
- [ ] Acceptance tests pass
- [ ] `trace` called with `WP-27` in `output_summary`
