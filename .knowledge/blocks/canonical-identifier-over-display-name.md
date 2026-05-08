# Canonical Identifier Over Display Name

- **id:** `canonical-identifier-over-display-name`
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
- rename
- wrong target
- lookup

## Dead ends

- resolve target from url slug alone
- use display name as stable identity
- write before canonical identifier is confirmed
- assume renamed resource keeps the same human label

## Procedure

1. Resolve the target through its canonical stable identifier.
2. Treat labels, URLs, and display names as discovery hints only.
3. Record the canonical identifier in the plan or mutation record.
4. Use that same identifier for the mutation and for post-change verification.
5. Escalate instead of guessing when multiple candidates match.

## Verification

- A canonical identifier was recorded before the write.
- The same identifier was used for readback.
- No ambiguity remained at execution time.

## Failure signals

- wrong target updated
- resource not found after rename
- ambiguous match set

## When not to apply

Pure read-only exploration where no state mutation or rollback will happen.

## Scope

- tool_patterns: api.write, db.write, deploy.apply
