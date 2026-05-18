# Agent OS Taste Invariants

These invariants exist to keep the repository legible to both humans and future
agent runs.

## Source of truth

- Do not guess API shapes. Read the defining schema or API surface first.
- Prefer typed boundaries over probing data structures "YOLO style".
- Keep architectural rules in live docs and back them with tests or checks.

## Repository shape

- Root host entrypoints stay short and point to the deeper docs tree.
- Plans, scorecards, decisions, and technical debt stay committed in-repo.
- Do not point live docs at nonexistent internal paths.

## Validation

- Match validation to the surface you changed.
- Frontend changes must run `cd frontend && npm run build` and `cd frontend && npm run test`.
- Python changes must run lint, type check, and tests through the existing project commands.
- Host instruction changes must regenerate the derived artifacts.

## Logging and evidence

- Prefer structured, queryable evidence over prose-only reassurance.
- Capture runtime evidence with scripts or tasks when validating service behavior.
- Keep verification steps reproducible by another agent without chat context.
