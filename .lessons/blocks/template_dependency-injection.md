# FastAPI Dependency Injection Boundaries

- **id:** `template-python-fastapi-dependency-injection`
- **domain:** `coding.python-fastapi`
- **status:** `active`
- **task_types:** feature, refactor

## Situation
A route, service, or test needs access to request-scoped resources such as sessions, auth principals, config, or clients.

## Triggers
- Depends
- request scoped dependency
- database session

## Dead ends
- Creating database sessions or network clients directly inside route handlers.
- Using module-level mutable singletons for request-scoped state.

## Procedure
1. Expose request-scoped resources through small FastAPI dependency functions.
2. Keep dependencies composable: authentication, session, and external clients should be separately overrideable in tests.
3. Use dependency_overrides in tests instead of monkeypatching global state.
4. Close or yield-clean up resources in the dependency that created them.

## Verification
- Run tests that override the dependency and prove the route uses the override.
- Check that resources created by yield dependencies are cleaned up after requests.

## Failure signals
- Tests need network access because dependencies cannot be overridden.
- Connection/session lifecycle is hidden in route code.

## Scope
- file_patterns: **/api/**/*.py, **/dependencies.py

## When not to apply
Simple pure functions that do not depend on request state or external resources.
