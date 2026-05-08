# Smallest Reviewable Change

- **id:** `smallest-reviewable-change`
- **domain:** `coding`
- **status:** `active`
- **task_types:** debugging, implementation, refactor

## Situation

Local fixes are easier to validate, review, and revert when the first change stays tightly scoped to one hypothesis and one behavior.

## Triggers

- minimal change
- reversible probe
- narrow patch
- reviewable diff

## Dead ends

- rewrite a large surface for a local bug
- mix cleanup with a functional fix
- change a public API without proving it is necessary
- make an irreversible edit before the path is validated

## Procedure

1. Limit the first edit to the smallest slice that can test the current hypothesis.
2. Keep unrelated cleanup, reformatting, and API changes out of the first patch.
3. Prefer a reversible probe when confidence is incomplete.
4. Add adjacent follow-up edits only after the first validation passes.

## Verification

- The diff matches a single clear intent.
- Unrelated formatting or cleanup was not mixed into the functional change.
- The first edit was narrow enough to review quickly.

## Failure signals

- reviewers must explain multiple problems to understand one fix
- the first patch changes unrelated modules or public APIs

## When not to apply

Large planned migrations where a narrow first slice would be misleading and a staged migration plan already exists.
