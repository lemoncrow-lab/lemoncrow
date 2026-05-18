# Plan Quality Gate

- **id:** `rb-swe-general-plan-quality`
- **domain:** `swe.general`
- **status:** `active`

## Situation
Before beginning any code generation, debugging, or refactoring task that requires a multi-step plan.


## Triggers
- plan
- task breakdown
- implementation

## Dead ends
- Jumping into implementation without validating the plan against requirements
- Skipping edge case analysis in favour of the happy path

## Procedure
1. State the goal in one sentence
2. List acceptance criteria before writing any code
3. Identify the smallest testable increment
4. Surface any ambiguity and resolve it before proceeding
