# Coding Conventions

**Analysis Date:** 2026-05-23

## Naming Patterns

**Files:**
- Use `snake_case.py` for Python modules in `src/atelier/**` (examples: `src/atelier/core/service/config.py`, `src/atelier/gateway/adapters/cli.py`).
- Use `PascalCase.tsx` for React pages/components and `camelCase.ts` for TS utilities in `frontend/src` (examples: `frontend/src/pages/Sessions.tsx`, `frontend/src/lib/insightsApi.ts`).
- Name Python tests as `test_*.py` under `tests/**` and frontend tests as `*.test.ts` / `*.test.tsx` beside source files (examples: `tests/core/test_models.py`, `frontend/src/pages/Sessions.test.tsx`).

**Functions:**
- Use `snake_case` for Python functions/methods (examples: `verify_api_key` in `src/atelier/core/service/auth.py`, `_normalize_lever` in `src/atelier/core/service/api.py`).
- Use `camelCase` for TS/TSX functions (examples: `buildTelemetryQuery` in `frontend/src/lib/insightsApi.ts`, `highlightSearchText` in `frontend/src/pages/Sessions.tsx`).

**Variables:**
- Use `snake_case` in Python locals/attributes (`store_path`, `runtime_store` in `src/atelier/core/service/api.py`).
- Use `camelCase` in TS locals/state (`searchParams`, `tracesRequestSeq` in `frontend/src/pages/Sessions.tsx`).
- Use `UPPER_SNAKE_CASE` for constants (`STABLE_LLM_TOOLS` in `src/atelier/core/environment.py`, `TELEMETRY_ACK_STORAGE_KEY` in `frontend/src/lib/insightsApi.ts`).

**Types:**
- Use `PascalCase` for Python classes/Pydantic models (`ServiceConfig` in `src/atelier/core/service/config.py`, `ReasonBlock` in `src/atelier/core/foundation/models.py`).
- Use `PascalCase` for TS interfaces/types (`TelemetryConfig` in `frontend/src/lib/insightsApi.ts`, `SessionSummary` usage in `frontend/src/pages/Sessions.tsx`).

## Code Style

**Formatting:**
- Python formatting uses Black (`[tool.black]` in `pyproject.toml`) and Ruff auto-fix (`Makefile` target `format`).
- Frontend formatting uses Prettier (`frontend/.prettierrc.json`), with `printWidth: 80`, `semi: true`, `singleQuote: false`.

**Linting:**
- Python linting uses Ruff (`[tool.ruff]` in `pyproject.toml`) with `select = ["E","F","I","B","UP","SIM","RUF"]`.
- Type checking uses strict mypy (`[tool.mypy] strict = true` in `pyproject.toml`).
- Frontend ESLint/Biome config is not detected at repo root or under `frontend/`; enforce TypeScript strictness via `frontend/tsconfig.json`.

## Import Organization

**Order:**
1. `from __future__ import annotations` first in Python modules (for example `src/atelier/core/service/config.py`, `src/atelier/gateway/adapters/cli.py`).
2. Python standard library imports.
3. Third-party imports (`click`, `yaml`, `fastapi`, `pytest`).
4. First-party `from atelier...` imports.

**Path Aliases:**
- Python uses absolute package imports rooted at `atelier` (example: `from atelier.core.service.config import cfg` in `src/atelier/core/service/api.py`).
- Frontend uses relative imports (`../api`, `./sessions/helpers` in `frontend/src/pages/Sessions.tsx`).
- TS path aliases are not configured (`frontend/tsconfig.json` has no `paths` entries).

## Error Handling

**Patterns:**
- Raise typed exceptions with explicit messages (`HTTPException` in `src/atelier/core/service/api.py`, `ValueError` in `src/atelier/core/environment.py`).
- Re-raise with chaining where appropriate (`raise HTTPException(...) from exc` in `src/atelier/core/service/api.py`).
- Convert lower-level failures into CLI-friendly exceptions (`click.ClickException` in `src/atelier/gateway/adapters/cli.py`).
- Frontend API wrapper throws on non-OK HTTP responses (`throw new Error(...)` in `frontend/src/lib/insightsApi.ts`).

## Logging

**Framework:** `logging` (Python stdlib)

**Patterns:**
- Define module-level loggers (`logger = logging.getLogger(__name__)`) in runtime modules (examples: `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/cli.py`).
- Log warnings with stack traces for degraded-but-recoverable paths (`logger.warning(..., exc_info=True)` in `src/atelier/core/service/api.py`).
- Keep secrets out of logs (explicit comment in `src/atelier/core/service/auth.py`).

## Comments

**When to Comment:**
- Use section banners for large modules (`# ------------------------------------------------------------------ #`) as seen in `src/atelier/core/service/api.py` and `src/atelier/gateway/adapters/cli.py`.
- Add rationale comments for non-obvious behavior (examples in `frontend/src/pages/Sessions.tsx` around history scope and search debounce).

**JSDoc/TSDoc:**
- Python uses docstrings heavily at module/class/function level (`src/atelier/core/service/auth.py`, `src/atelier/core/foundation/models.py`).
- Frontend mostly uses inline comments; TSDoc/JSDoc blocks are not a dominant pattern (`frontend/src/pages/Sessions.tsx`).

## Function Design

**Size:** Prefer helper extraction for repeated logic (examples: `_normalize_lever` in `src/atelier/core/service/api.py`, `request<T>` in `frontend/src/lib/insightsApi.ts`), but large orchestration modules (`src/atelier/gateway/adapters/cli.py`, `src/atelier/core/service/api.py`) are accepted.

**Parameters:** Use typed signatures throughout (strict typing in `pyproject.toml`; examples in `src/atelier/core/service/auth.py` and `frontend/src/lib/insightsApi.ts`).

**Return Values:** Return structured dict/model payloads instead of ad-hoc strings (examples: API route returns in `src/atelier/core/service/api.py`, typed promises in `frontend/src/lib/insightsApi.ts`).

## Module Design

**Exports:**  
- Use explicit public exports in SDK surface (`__all__` in `src/atelier/sdk/__init__.py`).
- Keep shared singleton config at module scope where needed (`cfg = ServiceConfig()` in `src/atelier/core/service/config.py`).

**Barrel Files:**  
- Python package-level re-export exists for SDK (`src/atelier/sdk/__init__.py`).
- Frontend barrel files (`index.ts`/`index.tsx`) are not a common pattern under `frontend/src`.

---

*Convention analysis: 2026-05-23*
