---
id: template-python-fastapi-pytest-layout
title: Pytest Layout For FastAPI Services
domain: coding.python-fastapi
task_types:
  - test
  - feature
triggers:
  - FastAPI test client
  - pytest fixture
  - endpoint test
file_patterns:
  - "tests/**/*.py"
  - "**/conftest.py"
tool_patterns:
  - pytest
situation: "A change adds or updates tests around FastAPI routes, dependencies, or service behavior. TODO: align fixture names with your repository."
dead_ends:
  - "Sharing mutable fixture state across tests without reset."
  - "Testing route internals instead of public request/response behavior."
procedure:
  - "Use a client fixture or factory that applies dependency overrides explicitly."
  - "Keep database fixtures isolated per test or roll them back predictably."
  - "Assert status codes and response bodies for both success and validation failure paths."
  - "Prefer narrow service fixtures over large app-wide fixtures when the endpoint is not under test."
verification:
  - "Run the focused test file first, then the broader API test subset."
  - "Confirm test order does not affect the result."
failure_signals:
  - "Tests pass alone but fail when the whole suite runs."
  - "Fixture setup performs real external calls."
required_rubrics: []
when_not_to_apply: "Unit tests for pure functions that do not need the app or test client."
---

TODO: Link to your preferred test command and fixture naming standard.
