# Cold Start Retrieval Trace Playbook

- **id:** `eval-cold-start-trace-playbook`
- **domain:** `coding`
- **status:** `active`

## Situation
When a retrieval pipeline needs candidate-level trace coverage.

## Triggers
- retrieval trace
- candidate count
- token budget

## Dead ends
- guessing why candidates disappeared without per-candidate evidence

## Procedure
1. Emit candidate count for every retrieval call
2. Record BM25, FTS, and base rank per block
3. Capture token_budget_evicted and wrong_domain drop reasons

## Verification
- retrieval trace includes candidate drop reasons

## Scope
- file_patterns: src/atelier/core/capabilities/context_reuse/**
- tool_patterns: search
