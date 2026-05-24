# Ruff And Black Formatting Gate

- **id:** `template-python-fastapi-ruff-black-formatting`
- **domain:** `coding.python-fastapi`
- **status:** `active`
- **task_types:** feature, refactor, test

## Situation
A Python code change is ready for validation.

## Triggers
- Ruff
- Black
- formatting
- lint

## Dead ends
- Declaring completion before local formatting/lint checks run.
- Hand-formatting around a formatter instead of using the configured tools.

## Procedure
1. Run the repository's formatter command or Black-compatible target.
2. Run Ruff linting on the touched files or project subset.
3. Fix reported issues in source rather than suppressing rules casually.
4. If a suppression is necessary, keep it narrow and explain it in code review.

## Verification
- Confirm formatter reports no diff.
- Confirm Ruff exits successfully on the touched files or documented subset.

## Failure signals
- CI formatting check changes files after the agent reports done.
- A broad noqa or per-file ignore hides unrelated issues.

## Scope
- file_patterns: **/*.py, pyproject.toml
- tool_patterns: ruff, black

## When not to apply
Documentation-only changes with no Python source edits.
