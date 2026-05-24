# Secret Handling In Service Code

- **id:** `template-python-fastapi-secret-handling`
- **domain:** `coding.python-fastapi`
- **status:** `active`
- **task_types:** feature, refactor, ops

## Situation
A change reads, stores, logs, tests, or passes credentials or sensitive tokens.

## Triggers
- API key
- environment variable
- secret
- token

## Dead ends
- Checking secrets into fixtures, examples, or snapshots.
- Logging Authorization headers or raw tokens.
- Using production-looking secrets in tests.

## Procedure
1. Read secrets from the approved runtime configuration source, not literals.
2. Validate required settings at startup with redacted error messages.
3. Use placeholder values in docs and tests.
4. Redact sensitive fields before logging, tracing, or returning errors.

## Verification
- Run secret scanning or the repository's configured security check.
- Inspect changed logs, exceptions, snapshots, and test fixtures for sensitive values.

## Failure signals
- A token-like string appears in committed files or test output.
- Errors reveal full credentials or connection strings.

## Scope
- file_patterns: **/*.py, **/.env*

## When not to apply
Changes that do not touch configuration, auth, logging, or external clients.
