# Testing Patterns

**Analysis Date:** 2026-05-18

## Test Framework

**Runner:**
- Python: `pytest` from `pyproject.toml`
  - Config: `pyproject.toml`
- Frontend: `vitest` from `frontend/package.json`
  - Config: `frontend/vite.config.ts`

**Assertion Library:**
- Python uses plain `assert`, `pytest.raises`, and fixture-based assertions in files such as `tests/gateway/test_service_api.py` and `tests/infra/test_postgres_store.py`.
- Frontend uses Vitest `expect` plus `@testing-library/jest-dom` from `frontend/src/test/setup.ts`.

**Run Commands:**
```bash
uv run pytest -q                               # Run all Python tests (`Makefile`)
uv run pytest --cov=atelier --cov-report=term-missing --cov-report=html  # Python coverage (`Makefile`)
make test-fast                                # Documented fast Python subset (`Makefile`)
cd frontend && npm test                        # Run frontend Vitest suite via `frontend/scripts/run-vitest.mjs`
```

## Test File Organization

**Location:**
- Python tests live in dedicated top-level folders under `tests/`: `tests/core/`, `tests/gateway/`, `tests/infra/`, and `tests/docs/`.
- Shared Python fixtures live in `tests/conftest.py`.
- Frontend tests are co-located beside source files in `frontend/src/**`, for example `frontend/src/pages/Reports.test.tsx` and `frontend/src/lib/insightsApi.test.ts`.

**Naming:**
- Python: `test_*.py`, for example `tests/core/test_repo_map.py`.
- Frontend: `*.test.ts` and `*.test.tsx`, for example `frontend/src/pages/Sessions.test.tsx`.

**Structure:**
```text
tests/
├── conftest.py
├── core/
├── gateway/
├── infra/
├── docs/
├── fixtures/
└── golden/

frontend/src/
├── lib/*.test.ts
├── pages/*.test.tsx
└── pages/sessions/*.test.tsx
```

## Test Structure

**Suite Organization:**
```python
# `tests/gateway/test_cli.py`
def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)

def test_run_rubric_via_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(root, "tools", "call", "verify", "--dev", "--json")
    assert res.exit_code == 0, res.output
```

```tsx
// `frontend/src/pages/Sessions.test.tsx`
describe("Sessions page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders session rows after load", async () => {
    mockFetch({ "/api/traces": jsonResponse(sampleTraces) });
    renderSessions();
    expect(await screen.findByText("Fix login bug")).toBeInTheDocument();
  });
});
```

**Patterns:**
- Python tests are mostly single-function arrange/act/assert flows with local helper builders instead of deep class hierarchies, as in `tests/core/test_repo_map.py` and `tests/core/service/test_api_week2_routes.py`.
- Frontend tests wrap components in only the providers they need, such as `MemoryRouter` and `TimeRangeProvider` in `frontend/src/pages/Sessions.test.tsx`.
- Teardown is lightweight: frontend suites call `vi.restoreAllMocks()` and sometimes `localStorage.clear()` in `afterEach`, while Python relies on fixture isolation from `tmp_path` and `monkeypatch`.

## Mocking

**Framework:** 
- Python: `pytest` fixtures plus `monkeypatch`
- Frontend: Vitest spies/mocks (`vi.spyOn`, `vi.mockImplementation`)

**Patterns:**
```python
# `tests/infra/test_postgres_store.py`
def test_postgres_store_no_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    import atelier.infra.storage.postgres_store as pg_mod

    original = pg_mod._psycopg
    try:
        pg_mod._psycopg = None
        with pytest.raises(RuntimeError, match="psycopg"):
            pg_mod.PostgresStore(database_url="postgresql://localhost/test")
    finally:
        pg_mod._psycopg = original
```

```tsx
// `frontend/src/lib/insightsApi.test.ts`
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

vi.spyOn(globalThis, "fetch").mockResolvedValue(
  jsonResponse(telemetryConfig({ acknowledged: false }))
);
```

**What to Mock:**
- Mock network boundaries in frontend tests by spying on `globalThis.fetch`, as in `frontend/src/pages/Insights.test.tsx`, `frontend/src/pages/Sessions.test.tsx`, and `frontend/src/pages/Reports.test.tsx`.
- Patch environment variables and optional dependencies in Python tests with `monkeypatch` and `pytest.importorskip`, as in `tests/gateway/test_service_api.py`, `tests/infra/test_postgres_store.py`, and `tests/gateway/test_mcp_remote_mode.py`.
- Monkeypatch expensive or unavailable backend calls rather than the surrounding orchestration, as in `tests/gateway/test_cli.py` replacing `atelier.core.capabilities.consolidation.worker.chat`.

**What NOT to Mock:**
- Do not mock core stores when a real temporary store is easy to create. Tests frequently instantiate `ContextStore` or `SQLiteStore` against `tmp_path`, for example `tests/conftest.py`, `tests/gateway/test_service_api.py`, and `tests/core/service/test_api_week2_routes.py`.
- Do not mock the FastAPI app surface for route tests. Build the real app with `create_app(...)` and exercise it with `TestClient`, as in `tests/gateway/test_service_api.py`.
- Do not mock rendered DOM structure when a direct Testing Library query is enough. Frontend tests assert on visible text and ARIA labels instead of component internals.

## Fixtures and Factories

**Test Data:**
```python
# `tests/conftest.py`
@pytest.fixture()
def store(tmp_path: Path) -> ContextStore:
    s = ContextStore(tmp_path / "atelier")
    s.init()
    return s
```

```python
# `tests/core/service/test_api_week2_routes.py`
def _write_trace(root: Path, session_id: str, *, model: str = "claude-sonnet-4-5") -> None:
    store = ContextStore(root)
    store.init()
    trace = Trace(
        id=f"trace-{session_id}",
        session_id=session_id,
        agent="copilot",
        domain="ui",
        task="Session UI audit",
        status="success",
        model=model,
    )
    store.record_trace(trace, write_json=False)
```

**Location:**
- Shared reusable fixtures live in `tests/conftest.py`.
- Scenario-specific factories stay local to each suite, for example `_run_snapshot`, `_write_trace`, `_write_imported_trace`, and `_build_imported_host_fixture` in `tests/core/service/test_api_week2_routes.py`.
- Static fixture assets live under `tests/fixtures/` and `tests/golden/`.

## Coverage

**Requirements:** No minimum coverage threshold is enforced in the checked-in config, but Python coverage is part of the documented command surface in `Makefile`.

**View Coverage:**
```bash
uv run pytest --cov=atelier --cov-report=term-missing --cov-report=html
```

## Test Types

**Unit Tests:**
- Pure logic tests dominate `tests/core/**` and `tests/infra/**`, using temporary files and direct function calls, for example `tests/core/test_repo_map.py` and `tests/infra/test_openmemory.py`.
- Frontend component/page tests are UI-level unit tests that mock fetch and assert rendered states, for example `frontend/src/pages/Insights.test.tsx`.

**Integration Tests:**
- CLI integration tests run the real Click command tree through `CliRunner`, for example `tests/gateway/test_cli.py`.
- API integration tests run the real FastAPI app in-process with `TestClient`, for example `tests/gateway/test_service_api.py`.
- Some live service tests start subprocesses and poll `/health`, for example `tests/gateway/test_live_production_validation.py` and `tests/gateway/test_mcp_remote_mode.py`.
- Docs and repo-governance checks are treated as tests under `tests/docs/`, for example `tests/docs/test_readme_no_unmeasured_claims.py`.

**E2E Tests:**
- Browser E2E frameworks such as Playwright or Cypress are not detected.
- Closest equivalent is process-level and container-level validation in `tests/gateway/test_live_production_validation.py`.

## Common Patterns

**Async Testing:**
```tsx
// `frontend/src/pages/Reports.test.tsx`
renderReports();
expect(await screen.findByText(/No reports published yet/i)).toBeInTheDocument();
```

- Frontend async tests prefer `findBy*` queries over manual polling.
- Python tests are mostly synchronous; when remote services are involved they poll explicitly, as in `_wait_for_health()` inside `tests/gateway/test_live_production_validation.py`.

**Error Testing:**
```python
# `tests/infra/test_postgres_store.py`
with pytest.raises(RuntimeError, match="psycopg"):
    pg_mod.PostgresStore(database_url="postgresql://localhost/test")
```

```tsx
// `frontend/src/pages/Sessions.test.tsx`
vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network error"));
renderSessions();
expect(await screen.findByText(/network error/i)).toBeInTheDocument();
```

- Python tests check failure paths explicitly with `pytest.raises`, `skipif`, and `importorskip`.
- Frontend tests verify loading, empty, success, and error states for the same component whenever feasible.
- `pytest.mark.slow` is used for heavier runtime tests such as `tests/gateway/test_live_production_validation.py`, and `Makefile` exposes a `test-fast` command that skips them.

---

*Testing analysis: 2026-05-18*
