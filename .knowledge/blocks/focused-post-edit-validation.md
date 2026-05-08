# Focused Post-Edit Validation

- **id:** `focused-post-edit-validation`
- **domain:** `coding`
- **status:** `active`
- **task_types:** debugging, implementation, refactor

## Situation

After an edit, the next useful action is usually a focused validation step. More reading or more patching before validation makes the failure surface larger and harder to reason about.

## Triggers

- run tests
- validate after edit
- rerun
- focused check
- typecheck
- lint

## Dead ends

- continue patching before running any validation
- use git diff as the first validation when a focused executable check exists
- widen scope before validating the touched slice

## Procedure

1. Run the cheapest behavior-scoped check that can falsify the current hypothesis.
2. If that is unavailable, run the narrowest test, compile, lint, or typecheck for the touched slice.
3. If the check fails, repair the same slice first and rerun the same check.
4. Only expand scope after the focused validation result is understood.

## Verification

- A focused validation command or test was run immediately after editing.
- The validation result was mapped back to the current hypothesis.
- Additional edits stayed adjacent to the validated slice.

## Failure signals

- more files are changed before any validation happens
- the first feedback loop comes from a broad suite instead of the touched slice

## When not to apply

Pure documentation or planning tasks with no executable validation surface.
