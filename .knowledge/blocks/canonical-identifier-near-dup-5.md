# Canonical Identifier Near Duplicate 5

- **id:** `canonical-identifier-near-dup-5`
- **domain:** `state.change`
- **status:** `active`
- **task_types:** integration_change, data_write, rollback

## Situation
Human-readable labels such as titles, URLs, paths, and display names can drift. Mutations and rollbacks should target a stable canonical identifier.


## Triggers
- slug
- handle
- title
- url

## Dead ends
- resolve target from url slug alone
- use display name as stable identity

## Procedure
1. Resolve the target through its canonical stable identifier.
2. Record that identifier before the write.
3. Use the same identifier for the mutation and the readback.

## Verification
- A canonical identifier was recorded before the write.
- The same identifier was used for readback.

## Failure signals
- wrong target updated
- ambiguous match set

## When not to apply
Pure read-only exploration where no state mutation or rollback will happen.


## Scope
- tool_patterns: api.write, db.write
