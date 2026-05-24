# Pydantic Models At API Boundaries

- **id:** `template-python-fastapi-pydantic-api-boundaries`
- **domain:** `coding.python-fastapi`
- **status:** `active`
- **task_types:** feature, refactor

## Situation
A FastAPI endpoint accepts or returns structured data.

## Triggers
- FastAPI request model
- Pydantic response model
- API schema change

## Dead ends
- Returning raw ORM objects directly from handlers.
- Letting dictionaries drift without a Pydantic schema.

## Procedure
1. Define explicit Pydantic request and response models for the endpoint boundary.
2. Keep persistence models separate from public API models unless the team has approved coupling.
3. Set response_model on the route and verify optional/null fields match the public contract.
4. When changing a model, update OpenAPI snapshots or schema tests if the repo has them.

## Verification
- Run the endpoint tests that exercise serialization and validation failures.
- Inspect the generated OpenAPI schema for changed public fields.

## Failure signals
- Response contains private database fields.
- 422 behavior changes without an explicit test.

## Scope
- file_patterns: **/api/**/*.py, **/schemas.py

## When not to apply
Pure internal helper changes that do not cross the API boundary.
