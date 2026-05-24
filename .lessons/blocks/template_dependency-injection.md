---
id: template-python-fastapi-dependency-injection
title: FastAPI Dependency Injection Boundaries
domain: coding.python-fastapi
task_types:
  - feature
  - refactor
triggers:
  - Depends
  - request scoped dependency
  - database session
file_patterns:
  - "**/api/**/*.py"
  - "**/dependencies.py"
tool_patterns: []
situation: "A route, service, or test needs access to request-scoped resources such as sessions, auth principals, config, or clients. TODO: document your approved dependency names."
dead_ends:
  - "Creating database sessions or network clients directly inside route handlers."
  - "Using module-level mutable singletons for request-scoped state."
procedure:
  - "Expose request-scoped resources through small FastAPI dependency functions."
  - "Keep dependencies composable: authentication, session, and external clients should be separately overrideable in tests."
  - "Use dependency_overrides in tests instead of monkeypatching global state."
  - "Close or yield-clean up resources in the dependency that created them."
verification:
  - "Run tests that override the dependency and prove the route uses the override."
  - "Check that resources created by yield dependencies are cleaned up after requests."
failure_signals:
  - "Tests need network access because dependencies cannot be overridden."
  - "Connection/session lifecycle is hidden in route code."
required_rubrics: []
when_not_to_apply: "Simple pure functions that do not depend on request state or external resources."
---

TODO: Add company-specific dependency override examples here.
