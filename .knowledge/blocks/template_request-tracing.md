---
id: template-python-fastapi-request-tracing
title: Request Tracing And Correlation IDs
domain: coding.python-fastapi
task_types:
  - feature
  - debug
  - ops
triggers:
  - correlation ID
  - request ID
  - tracing
  - middleware
file_patterns:
  - "**/middleware*.py"
  - "**/api/**/*.py"
  - "**/*.py"
tool_patterns: []
situation: "A request path, middleware, or downstream client needs traceability across service boundaries. TODO: name your canonical header and trace field."
dead_ends:
  - "Generating a new request ID when a trusted upstream ID already exists."
  - "Dropping correlation IDs before downstream calls or background jobs."
procedure:
  - "Read the approved inbound correlation header or create one at the service edge."
  - "Attach the ID to request-local context used by logs and errors."
  - "Propagate the ID to approved downstream calls and background tasks."
  - "Return or expose the ID according to the service's debugging policy."
verification:
  - "Test requests with and without an inbound correlation ID."
  - "Inspect logs or response headers to confirm the same ID is preserved."
failure_signals:
  - "A single user request produces unrelated IDs across logs."
  - "Background work cannot be tied back to the originating request."
required_rubrics: []
when_not_to_apply: "Offline scripts and one-shot migrations that do not handle service requests."
---

TODO: Add your trace header name and any privacy constraints.
