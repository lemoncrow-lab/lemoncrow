# Legacy Retrieval Trace Playbook 0

- **id:** `eval-legacy-trace-playbook-0`
- **domain:** `coding`
- **status:** `active`

## Situation
When adding generic retrieval trace logging.

## Triggers
- retrieval trace
- candidate count
- token budget

## Dead ends
- adding logs without rank attribution

## Procedure
1. Add generic retrieval trace logs
2. Print candidate information without drop reasons

## Verification
- logs emitted

## Scope
- file_patterns: src/atelier/core/capabilities/context_reuse/**
- tool_patterns: search
