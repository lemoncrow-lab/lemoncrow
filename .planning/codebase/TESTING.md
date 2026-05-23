# Testing Patterns

**Analysis Date:** 2026-05-23

## Test Framework

**Runner:**
- Python: `pytest` (configured in `pyproject.toml` under `[tool.pytest.ini_options]`)
- Frontend: `vitest` via `frontend/scripts/run-vitest.mjs` and `frontend/vite.config.ts`
- Config: `pyproject.toml`, `frontend/vite.config.ts`, `frontend/src/test/setup.ts`

**Assertion Library:**
- Python: built-in `assert` + `pytest.raises` (example: `tests/core/test_models.py`)
- Frontend: `@testing-library/jest-dom` matchers + Vitest `expect` (example: `frontend/src/pages/Sessions.test.tsx`)

**Run Commands:**
```bash
make test                  # Run Python test suite
make test-fast             # Fast Python subset (-x, skips selected files)
make test-cov              # Python coverage report (term + htmlcov)
cd frontend && npm test    # Frontend Vitest run via run-vitest.mjs
```

## Test File Organization

**Location:**
- Python tests are separate under `tests/` and grouped by domain (`tests/core/`, `tests/gateway/`, `tests/infra/`, `tests/docs/`, `tests/benchmarks/`).
- Frontend tests are co-located with source files in `frontend/src/**`.

**Naming:**
- Python: `test_*.py` (examples: `tests/core/test_models.py`, `tests/gateway/test_cli.py`)
- Frontend: `*.test.ts` / `*.test.tsx` (examples: `frontend/src/lib/insightsApi.test.ts`, `frontend/src/pages/Watchdogs.test.tsx`)

**Structure:**
```
tests/
  conftest.py
  core/
  gateway/
  infra/
  benchmarks/
  fixtures/
frontend/src/
  ...feature files...
  *.test.ts(x)
  test/setup.ts
```

## Test Structure

**Suite Organization:**
```typescript
describe("Sessions page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders session rows after load", async () => {
    // arrange mock fetch
    // render component
    // assert via findBy/getBy
  });
});
```
Pattern source: `frontend/src/pages/Sessions.test.tsx`.

**Patterns:**
- Setup pattern: helper builders for fixtures and runtime state (`_write_trace`, `_write_run` in `tests/core/service/test_api_week2_routes.py`).
- Teardown pattern: `afterEach(() => vi.restoreAllMocks())` in frontend tests (`frontend/src/pages/Traces.test.tsx`, `frontend/src/pages/Watchdogs.test.tsx`).
- Assertion pattern: explicit message-aware assertions and response parsing (`assert res.exit_code == 0, res.output` in `tests/gateway/test_cli.py`).

## Mocking

**Framework:** `pytest` fixtures + `monkeypatch` + `unittest.mock.patch` (Python), `vi.spyOn`/Vitest mocks (frontend)

**Patterns:**
```python
@pytest.fixture(autouse=True)
def _no_network_sync() -> Iterator[None]:
    with patch("atelier.core.service.usage_sync.sync_usage", return_value=True):
        yield
```
Source: `tests/conftest.py`.

```typescript
vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
  // return Response per URL
});
```
Source: `frontend/src/pages/Watchdogs.test.tsx`.

**What to Mock:**
- External network/LLM boundaries (`tests/conftest.py` blocks sync usage and Ollama calls).
- Environment variables and integration adapters (`monkeypatch.setenv` / `monkeypatch.setattr` in `tests/infra/test_openmemory.py`, `tests/gateway/test_mcp_jsonrpc_e2e.py`).
- Browser `fetch` in frontend page/API tests (`frontend/src/**/*.test.tsx`).

**What NOT to Mock:**
- Core model validation behavior (`tests/core/test_models.py` uses real `ReasonBlock`, `Trace`, `Rubric`).
- CLI argument parsing path (tests invoke real Click command via `CliRunner` in `tests/gateway/test_cli.py`).

## Fixtures and Factories

**Test Data:**
```python
trace = Trace(
    id=f"trace-{session_id}",
    session_id=session_id,
    agent="copilot",
    domain="ui",
    task="Session UI audit",
    status="success",
)
store.record_trace(trace, write_json=False)
```
Source: `tests/core/service/test_api_week2_routes.py`.

**Location:**
- Shared Python fixtures: `tests/conftest.py`
- Static fixture payloads: `tests/fixtures/` and `tests/fixtures/golden/`
- Frontend helper setup: `frontend/src/test/setup.ts`

## Coverage

**Requirements:** No `--cov-fail-under` threshold is enforced in repo config (`pyproject.toml` and `Makefile`).

**View Coverage:**
```bash
make test-cov
```

## Test Types

**Unit Tests:**
- Validate pure/domain logic and model constraints (examples: `tests/core/test_models.py`, `tests/core/capabilities/prompt_compilation/test_compiler.py`).

**Integration Tests:**
- Exercise API/CLI/MCP flows with temporary stores and real command dispatch (examples: `tests/core/service/test_api_week2_routes.py`, `tests/gateway/test_mcp_jsonrpc_e2e.py`).

**E2E Tests:**
- Present and marked `slow` when subprocess boundaries are involved (example: `tests/gateway/test_mcp_jsonrpc_e2e.py`).
- Frontend component integration tests run in `jsdom` (`frontend/vite.config.ts`).

## Common Patterns

**Async Testing:**
```typescript
await waitFor(() => {
  expect(screen.getByText("Baseline session")).toBeInTheDocument();
});
expect(await screen.findByText("Search shell timeout")).toBeInTheDocument();
```
Source: `frontend/src/pages/Traces.test.tsx`.

**Error Testing:**
```python
with pytest.raises(ValidationError):
    ReasonBlock(..., extra_field="nope")
```
Source: `tests/core/test_models.py`.

```typescript
vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network error"));
expect(await screen.findByText(/network error/i)).toBeInTheDocument();
```
Source: `frontend/src/pages/Sessions.test.tsx`.

---

*Testing analysis: 2026-05-23*
