# Fix Authoritative Source First

- **id:** `fix-authoritative-source-first`
- **domain:** `source.truth`
- **status:** `active`
- **task_types:** bug_fix, sync_fix, content_fix

## Situation
When a rendered or downstream artifact is wrong, fixing only the visible copy often creates a temporary patch that will be overwritten by the next sync, build, or publish step.


## Triggers
- source of truth
- drift back
- sync overwrite
- regenerate
- upstream source

## Dead ends
- patch the derived output directly and stop there
- accept a manual fix that will be overwritten on the next refresh
- backfill the source from a stale render

## Procedure
1. Locate the authoritative source for the incorrect value or behavior.
2. Fix the issue at that source.
3. Regenerate or republish downstream artifacts from the corrected source.
4. Verify the fix survives the next sync, rebuild, or refresh.

## Verification
- The authoritative source changed.
- Downstream artifacts were refreshed or republished.
- The fix did not drift back on the next refresh.

## Failure signals
- fix disappears after next sync
- multiple copies disagree again immediately
- the same hotfix must be re-applied

## When not to apply
Tasks that explicitly target a release artifact as the source of record.
