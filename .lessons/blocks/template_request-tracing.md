# Request Tracing And Correlation IDs

- **id:** `template-python-fastapi-request-tracing`
- **domain:** `coding.python-fastapi`
- **status:** `active`
- **task_types:** feature, debug, ops

## Situation
A request path, middleware, or downstream client needs traceability across service boundaries.

## Triggers
- correlation ID
- request ID
- tracing
- middleware

## Dead ends
- Generating a new request ID when a trusted upstream ID already exists.
- Dropping correlation IDs before downstream calls or background jobs.

## Procedure
1. Read the approved inbound correlation header or create one at the service edge.
2. Attach the ID to request-local context used by logs and errors.
3. Propagate the ID to approved downstream calls and background tasks.
4. Return or expose the ID according to the service's debugging policy.

## Verification
- Test requests with and without an inbound correlation ID.
- Inspect logs or response headers to confirm the same ID is preserved.

## Failure signals
- A single user request produces unrelated IDs across logs.
- Background work cannot be tied back to the originating request.

## Scope
- file_patterns: **/middleware*.py, **/api/**/*.py, **/*.py

## When not to apply
Offline scripts and one-shot migrations that do not handle service requests.
