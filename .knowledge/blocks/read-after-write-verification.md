# Read-After-Write Verification

- **id:** `read-after-write-verification`
- **domain:** `state.change`
- **status:** `active`
- **task_types:** integration_change, deploy, migration, config_change

## Situation
A success response from a write path is not proof that observed state changed as intended. Verification must happen through the authoritative read surface.


## Triggers
- write
- update
- publish
- mutate
- sync
- migration
- deploy

## Dead ends
- trust success response as end state
- skip readback because the change seems small
- validate intent without observing actual state

## Procedure
1. Capture pre-change state when the risk level makes recovery relevant.
2. Apply the mutation.
3. Re-read the affected state through the authoritative interface.
4. Diff observed state against intended state.
5. Stop and recover or escalate if drift remains.

## Verification
- Observed state matches intent.
- Pre-change state was captured when rollback matters.
- A downstream or user-visible check passed for risky changes.

## Failure signals
- write succeeded but state did not change
- drift detected after mutation
- partial update observed

## When not to apply
Read-only tasks or disposable local scratch changes with no lasting state.


## Scope
- tool_patterns: api.write, db.write, deploy.apply
