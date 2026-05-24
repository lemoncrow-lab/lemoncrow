---
id: template-python-fastapi-secret-handling
title: Secret Handling In Service Code
domain: coding.python-fastapi
task_types:
  - feature
  - refactor
  - ops
triggers:
  - API key
  - environment variable
  - secret
  - token
file_patterns:
  - "**/*.py"
  - "**/.env*"
tool_patterns: []
situation: "A change reads, stores, logs, tests, or passes credentials or sensitive tokens. TODO: replace with your secret manager and redaction policy."
dead_ends:
  - "Checking secrets into fixtures, examples, or snapshots."
  - "Logging Authorization headers or raw tokens."
  - "Using production-looking secrets in tests."
procedure:
  - "Read secrets from the approved runtime configuration source, not literals."
  - "Validate required settings at startup with redacted error messages."
  - "Use placeholder values in docs and tests."
  - "Redact sensitive fields before logging, tracing, or returning errors."
verification:
  - "Run secret scanning or the repository's configured security check."
  - "Inspect changed logs, exceptions, snapshots, and test fixtures for sensitive values."
failure_signals:
  - "A token-like string appears in committed files or test output."
  - "Errors reveal full credentials or connection strings."
required_rubrics: []
when_not_to_apply: "Changes that do not touch configuration, auth, logging, or external clients."
---

TODO: Document the exact secret scanner command used by your team.
