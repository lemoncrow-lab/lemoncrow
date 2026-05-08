# Change Gate Discipline

- **id:** `change-gate-discipline`
- **domain:** `policy.change`
- **status:** `active`
- **task_types:** policy_change, scoring_change, classifier_change

## Situation

Thresholds, weights, rules, and classification logic can silently change behavior for many users at once. They need baseline capture and before-and-after review, not ad-hoc tuning.

## Triggers

- threshold
- weight
- heuristic
- scoring
- audit
- rule change
- ranking
- classifier

## Dead ends

- tweak thresholds without a baseline
- change weights without a before and after diff
- remove a noisy check without regression evidence
- hide a policy change inside unrelated cleanup

## Procedure

1. Capture baseline behavior on a representative regression set.
2. Apply the change behind an explicit version or documented change point.
3. Re-run the regression set.
4. Review flips, outliers, and user-visible changes.
5. Update the version marker or changelog.

## Verification

- Baseline and after snapshots were preserved.
- Regression diff was reviewed.
- Version or changelog was updated.

## Failure signals

- user-visible decisions flip unexpectedly
- no one can explain the output change
- regression set is missing

## When not to apply

Pure refactors that do not change output behavior or decision logic.
