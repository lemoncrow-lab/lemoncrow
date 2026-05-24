---
id: template-python-fastapi-structured-logging
title: Structured Logging For Requests
domain: coding.python-fastapi
task_types:
  - feature
  - refactor
  - debug
triggers:
  - logging
  - request log
  - error handling
file_patterns:
  - "**/*.py"
tool_patterns: []
situation: "A change adds logs or changes request/error observability in a FastAPI service. TODO: adapt field names to your logging platform."
dead_ends:
  - "Adding print statements or unstructured string blobs in request paths."
  - "Logging request bodies without explicit redaction."
procedure:
  - "Log stable event names with structured fields rather than interpolated strings."
  - "Include correlation/request identifiers when available."
  - "Record outcome, status, and latency where they help operations debug behavior."
  - "Exclude secrets, tokens, and high-cardinality payloads."
verification:
  - "Run tests that exercise the logging path if the repo captures logs."
  - "Inspect a sample log line for field names, redaction, and correlation ID presence."
failure_signals:
  - "Logs are not searchable by event name or request ID."
  - "Sensitive payloads appear in plain text logs."
required_rubrics: []
when_not_to_apply: "Internal pure calculations where logging would add noise."
---

TODO: Add examples of accepted log fields for your service.
