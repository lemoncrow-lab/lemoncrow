## Engineering Style
Hard-remove strategy: never deprecate, just delete.
- Use strict mypy mode. This is a requirement.
- All functions must have return type annotations.

## Telemetry Stack
OTel traces sent to PostHog and GCP.

## Project Setup
- Run `uv sync` to install dependencies.
- Tests are in the `tests/` directory.
