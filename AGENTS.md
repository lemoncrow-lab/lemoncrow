# Project Instructions: Atelier

## General Agent Guidelines
- **No Shortcuts:** Never assume the structure of data or APIs. Always verify the source of truth (e.g., `api.ts` for frontend, `schemas.py` or `models.py` for backend) before implementation.
- **Strict Verification:** Always run the project's build, lint, and type-check commands after making changes. A task is not complete until it passes all validation steps.
- **Atelier Integration:** Use `mcp_atelier_record` after every significant fix to record the outcome and help the system learn.

## Frontend Engineering Standards (TypeScript/React)

### API Interaction
- **Explicit Return Types:** Always verify the return type of API methods in `frontend/src/api.ts` before using the response. Do not assume raw arrays are returned.
- **Traces API:** `api.traces()` returns a `TraceListResponse` object, not a `Trace[]` array.
  - Correct usage: `api.traces(...).then(res => setItems(res.items))`
  - Error-prone shortcut: `api.traces(...).then(res => setItems(res))` (This causes TypeScript errors in state setters).
- **TypeScript Strictness:** Never use implicit `any` in callback parameters (e.g., in `.map()`, `.filter()`). Always provide an explicit type if the compiler cannot infer it.

### Workflow
- **Validation:** Always run `bun run build` or `npm run typecheck` in the `frontend` directory after modifying frontend code.

## Backend Engineering Standards (Python)

### Core Principles
- **Type Hints:** Use strict type hints for all function signatures and variable declarations. The project uses `mypy --strict`.
- **FastAPI Schemas:** Use Pydantic models in `src/atelier/core/service/schemas.py` for all API request and response bodies.
- **Service API:** When modifying `src/atelier/core/service/api.py`, ensure that any new endpoints or logic adhere to existing dependency injection and error handling patterns.
- **No nested `if` guards (SIM102):** Never write `if cond:\n    if hit := expr:`. Combine into `if cond and (hit := expr):`. The pre-commit ruff check enforces SIM102 and will block commits.

### Tooling & Workflow
- **Linting & Formatting:** Use `ruff check . --fix` and `ruff format .` for all Python changes.
- **Type Checking:** Run `mypy src` to verify type safety.
- **Testing:** Add or update tests in the `tests/` directory and run them with `pytest`.
- **Dependency Management:** Use `uv` for managing dependencies (e.g., `uv sync`, `uv add`).
