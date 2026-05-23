# Coding Conventions

**Analysis Date:** 2026-05-18

## Naming Patterns

**Files:**
- Use `snake_case.py` for Python modules under `src/atelier/**` and `tests/**`, for example `src/atelier/core/environment.py` and `tests/gateway/test_service_api.py`.
- Use `PascalCase.tsx` for React components and pages under `frontend/src/**`, for example `frontend/src/App.tsx`, `frontend/src/pages/Reports.tsx`, and `frontend/src/components/WorkbenchUI.tsx`.
- Use `camelCase.ts` for frontend utility/API modules, for example `frontend/src/lib/insightsApi.ts` and `frontend/src/lib/utils.ts`.
- Name Python tests `test_*.py` in tiered folders such as `tests/core/`, `tests/gateway/`, `tests/infra/`, and `tests/docs/`. Name frontend tests `*.test.ts` or `*.test.tsx` beside the implementation, such as `frontend/src/pages/Sessions.test.tsx`.

**Functions:**
- Use `snake_case` for Python functions and private helpers, with leading underscores for module-private helpers such as `_canonical_json` in `src/atelier/core/capabilities/code_context/engine.py` and `_bool_env` in `src/atelier/core/service/config.py`.
- Use `camelCase` for TypeScript functions such as `getTelemetryConfig`, `markLocalTelemetryAcknowledged`, and `renderReports` in `frontend/src/lib/insightsApi.ts` and `frontend/src/pages/Reports.test.tsx`.
- Use `PascalCase` for React components and Python classes, for example `CodeContextEngine` in `src/atelier/core/capabilities/code_context/engine.py`, `ServiceConfig` in `src/atelier/core/service/config.py`, and `Reports` in `frontend/src/pages/Reports.tsx`.

**Variables:**
- Use `snake_case` for Python locals, parameters, and fixtures such as `seeded_runtime`, `tmp_path`, and `app_no_auth` in `tests/conftest.py` and `tests/gateway/test_service_api.py`.
- Use `camelCase` for frontend state and locals such as `contentErr`, `selected`, and `sampleSessions` in `frontend/src/pages/Reports.tsx` and `frontend/src/pages/Sessions.test.tsx`.
- Use `UPPER_SNAKE_CASE` for constants in both languages, for example `TRACE_FTS_COLUMNS` in `src/atelier/core/foundation/store.py`, `DEV_MODE_ENV_VAR` in `src/atelier/core/environment.py`, and `NAV_ITEMS` in `frontend/src/App.tsx`.

**Types:**
- Use `PascalCase` for Pydantic models, interfaces, and aliases such as `ReasonBlock` in `src/atelier/core/foundation/models.py`, `ContextRequest` in `src/atelier/core/service/schemas.py`, and `TelemetryConfig` in `frontend/src/lib/insightsApi.ts`.
- Use explicit literal/type aliases for constrained domains, for example `BlockStatus`, `TraceStatus`, and `Severity` in `src/atelier/core/foundation/models.py`.

## Code Style

**Formatting:**
- Python uses Ruff and Black from `pyproject.toml`.
- Ruff is configured at `pyproject.toml` with `line-length = 100`, `target-version = "py311"`, and lint families `E`, `F`, `I`, `B`, `UP`, `SIM`, and `RUF`.
- Black is configured at `pyproject.toml` with `line-length = 120`.
- Frontend formatting uses Prettier from `frontend/.prettierrc.json` with 2-space indentation, semicolons, double quotes, trailing commas `es5`, and `printWidth = 80`.
- `Makefile` routes formatting through `make format`, which runs `ruff --fix`, `black`, and `npx prettier --write` for `frontend/src/**/*.{ts,tsx,js,jsx,json,css,md}`.

**Linting:**
- Python linting is enforced with Ruff via `make lint` in `Makefile`.
- Python type-checking is enforced with strict mypy via `make typecheck` and `[tool.mypy] strict = true` in `pyproject.toml`.
- Per-module mypy exceptions are explicit and localized, for example `src/atelier/gateway/adapters/cli.py` is listed under `ignore_errors = true` and API decorator-heavy modules are listed under `disable_error_code = ["untyped-decorator"]` in `pyproject.toml`.
- No standalone frontend ESLint or Biome configuration is detected. Frontend quality gates rely on `frontend/tsconfig.json` strict compiler options plus Vitest tests.

## Import Organization

**Order:**
1. `from __future__ import annotations` first in Python modules, for example `src/atelier/core/foundation/models.py`, `src/atelier/core/environment.py`, and `tests/gateway/test_cli.py`
2. Standard library imports
3. Third-party imports
4. Local `atelier.*` imports or relative frontend imports

**Path Aliases:**
- No TypeScript path aliases are detected in `frontend/tsconfig.json`.
- Frontend code uses relative imports such as `../lib/TimeRangeContext` in `frontend/src/pages/Sessions.test.tsx` and `./pages/Reports` in `frontend/src/App.tsx`.
- Python code imports by package path from `atelier.*`, for example `from atelier.core.foundation.models import ReasonBlock` in `src/atelier/gateway/adapters/cli.py`.

## Error Handling

**Patterns:**
- Raise specific Python exceptions for invalid inputs and unsupported runtime states, for example `raise ValueError("ATELIER_PROFILE must be 'stable' or 'dev'")` in `src/atelier/core/environment.py` and validator errors in `src/atelier/core/foundation/models.py`.
- Use `try/except` around optional dependencies and environment-sensitive behavior, returning safe fallbacks instead of crashing, for example `_git_repo_class()` in `src/atelier/core/capabilities/code_context/engine.py` and `_atelier_version()` in `src/atelier/gateway/adapters/cli.py`.
- Keep frontend API failure behavior centralized: `request<T>()` in `frontend/src/lib/insightsApi.ts` throws on non-OK responses, while pages decide whether to surface or suppress the error.
- Swallow non-critical UI failures only when loss is acceptable, for example telemetry acknowledgement and config reads in `frontend/src/App.tsx` use `.catch(() => undefined)`.
- Surface user-visible frontend failures through component state instead of logging, for example `setErr(String(e))` and `setContentErr(String(e))` in `frontend/src/pages/Reports.tsx`.

## Logging

**Framework:** Python `logging`; frontend generally avoids runtime logging in the application code sampled.

**Patterns:**
- Define module-scoped loggers with `logging.getLogger(__name__)`, for example in `src/atelier/gateway/adapters/cli.py` and `src/atelier/core/foundation/store.py`.
- Prefer structured return values, stored traces, or HTTP responses over ad-hoc logs for normal control flow.
- Avoid `console.log` in frontend modules reviewed under `frontend/src/**`; UI state and rendered alerts are preferred.

## Comments

**When to Comment:**
- Use module docstrings to declare responsibility and constraints, for example `src/atelier/core/environment.py`, `src/atelier/core/foundation/models.py`, and `src/atelier/gateway/adapters/cli.py`.
- Use section dividers in long Python modules and tests to separate concerns, such as `# --------------------------------------------------------------------------- #` blocks in `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/foundation/store.py`, and `tests/gateway/test_service_api.py`.
- Keep inline comments sparse and purpose-driven. Frontend comments mostly explain UX or edge-case intent, such as the storage fallback note in `frontend/src/lib/insightsApi.ts` and section labels in `frontend/src/pages/Reports.tsx`.

**JSDoc/TSDoc:**
- Python docstrings are common at module, class, and selected method level.
- JSDoc/TSDoc usage is minimal in `frontend/src/**`; TypeScript relies more on interfaces and explicit prop typing than comment blocks.

## Function Design

**Size:** Favor small helpers around a typed core, but allow large command/router modules when grouping related surfaces. `src/atelier/gateway/adapters/cli.py` is the main exception; most other modules, such as `src/atelier/core/service/config.py` and `frontend/src/lib/utils.ts`, keep functions narrow.

**Parameters:**
- Annotate Python parameters and returns consistently, including helpers and tests, as seen in `tests/gateway/test_cli.py`, `tests/core/service/test_api_week2_routes.py`, and `src/atelier/core/capabilities/code_context/engine.py`.
- Use keyword-only parameters in Python when optional behavior matters, for example `index_repo(..., include_globs=..., exclude_globs=..., force=True)` in `src/atelier/core/capabilities/code_context/engine.py`.
- Use typed props objects and generics in TypeScript, for example `request<T>()` in `frontend/src/lib/insightsApi.ts` and prop signatures in `frontend/src/components/WorkbenchUI.tsx`.

**Return Values:**
- Prefer Pydantic models for domain contracts in Python core code, for example `ReasonBlock` and `Trace` in `src/atelier/core/foundation/models.py`.
- Use plain dictionaries only for boundary layers or lightweight config/report payloads, for example `ServiceConfig.as_dict()` in `src/atelier/core/service/config.py`.
- Return typed `Promise<T>` values from frontend API helpers and keep the parsing step centralized in the helper module, as in `frontend/src/lib/insightsApi.ts`.

## Module Design

**Exports:** 
- Python modules export concrete classes/functions directly; consumers import from the owning module instead of broad barrel modules, for example `src/atelier/core/foundation/models.py` and `src/atelier/core/service/config.py`.
- Frontend pages default-export the page component, for example `frontend/src/pages/Reports.tsx` and `frontend/src/pages/Insights.tsx`.
- Shared frontend primitives use named exports, for example `frontend/src/components/WorkbenchUI.tsx` exports `Alert`, `Button`, `Card`, `Select`, and `ToggleGroup`.

**Barrel Files:** 
- Frontend uses a selective barrel-style module in `frontend/src/components/WorkbenchUI.tsx` to re-export UI primitives and helper components.
- Python `__init__.py` files mostly mark packages rather than acting as large re-export layers, as seen across `src/atelier/**/__init__.py`.

**Architecture conventions:**
- Preserve the package split between `src/atelier/core/**` for domain models/policies, `src/atelier/gateway/**` for adapters and host-facing surfaces, and `src/atelier/infra/**` for persistence/runtime integrations.
- Keep API schemas separate from core models: `src/atelier/core/service/schemas.py` wraps service boundary shapes while `src/atelier/core/foundation/models.py` holds runtime contracts.
- Keep tests aligned to the same tier split: `tests/core/**`, `tests/gateway/**`, `tests/infra/**`, and `tests/docs/**`.

---

*Convention analysis: 2026-05-18*
