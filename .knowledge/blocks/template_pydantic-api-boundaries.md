---
id: template-python-fastapi-pydantic-api-boundaries
title: Pydantic Models At API Boundaries
domain: coding.python-fastapi
task_types:
  - feature
  - refactor
triggers:
  - FastAPI request model
  - Pydantic response model
  - API schema change
file_patterns:
  - "**/api/**/*.py"
  - "**/schemas.py"
tool_patterns: []
situation: "A FastAPI endpoint accepts or returns structured data. TODO: adapt examples to your service naming conventions."
dead_ends:
  - "Returning raw ORM objects directly from handlers."
  - "Letting dictionaries drift without a Pydantic schema."
procedure:
  - "Define explicit Pydantic request and response models for the endpoint boundary."
  - "Keep persistence models separate from public API models unless the team has approved coupling."
  - "Set response_model on the route and verify optional/null fields match the public contract."
  - "When changing a model, update OpenAPI snapshots or schema tests if the repo has them."
verification:
  - "Run the endpoint tests that exercise serialization and validation failures."
  - "Inspect the generated OpenAPI schema for changed public fields."
failure_signals:
  - "Response contains private database fields."
  - "422 behavior changes without an explicit test."
required_rubrics: []
when_not_to_apply: "Pure internal helper changes that do not cross the API boundary."
---

TODO: Replace this note with examples from your service once the template is adopted.
