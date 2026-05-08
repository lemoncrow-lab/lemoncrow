# Concrete Anchor Before Edit

- **id:** `concrete-anchor-before-edit`
- **domain:** `coding`
- **status:** `active`
- **task_types:** debugging, implementation, refactor

## Situation

Broad repo exploration is expensive and often misses the controlling code path. Start from the most concrete failing surface, then move only one hop to the code that directly decides the behavior.

## Triggers

- failing test
- failing command
- failing behavior
- concrete anchor
- control path
- local hypothesis

## Dead ends

- map the whole codebase before choosing a hypothesis
- edit multiple files before identifying the controlling code
- keep searching without a discriminating check
- compare nearby paths without choosing one to test

## Procedure

1. Start from a concrete anchor such as a failing test, command, file, symbol, or behavior.
2. Step to the nearest code that directly computes or controls the behavior.
3. State one falsifiable local hypothesis about the failure or requested change.
4. Choose the cheapest nearby check that could disconfirm that hypothesis.
5. Make the smallest edit or reversible probe that tests the chosen path.

## Verification

- A controlling code path was identified.
- One falsifiable local hypothesis was written down.
- One discriminating check was chosen before widening scope.

## Failure signals

- repo exploration keeps expanding without a chosen code path
- several plausible paths are compared without a test

## When not to apply

Pure greenfield design or architecture work where there is no concrete failing surface yet.
