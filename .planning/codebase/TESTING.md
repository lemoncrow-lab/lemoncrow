# Testing Patterns

**Analysis Date:** 2026-05-28

## Test Framework

**Runner:**
- `pytest` ≥ 9.0 (dev dependency group in `pyproject.toml`)
- Config: `pyproject.toml` under `[tool.pytest.ini_options]`
- Parallel execution via `pytest-xdist` ≥ 3.8.0

**Key pytest configuration:**
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers -m 'not slow'"
pythonpath = ["src", "."]
markers = [
    "slow: marks tests as slow",
    "ab: real A/B benchmark; writes calibration data",
]
```
- Default run excludes `slow` and `ab` marked tests
- `--strict-markers` enforces that all marks are declared

**Assertion Library:**
- Standard `pytest` assertions (no separate library)

**Run Commands:**
```bash
make test               # Run all (non-slow) tests, parallel if xdist available
make test-fast          # Stop on first failure, skip postgres/worker tests
make test-cov           # Run with coverage report
make security-test      # Run gateway security tests only
uv run pytest -q -ra    # Direct pytest run
uv run pytest --cov=atelier --cov-report=term-missing --cov-report=html  # With coverage
uv run pytest -q -ra --durations=0 -n auto --dist=loadfile  # Parallel run
```

## Test File Organization

**Location:**
- All tests live in `tests/` (separate from `src/`) — NOT co-located with source
- `tests/` mirrors the `src/atelier/` layer structure

**Directory structure:**
```
tests/
├── conftest.py                     # Shared fixtures (autouse isolation)
├── fixtures/                       # Static test data files
│   ├── 200_failed_traces.jsonl
│   ├── archival_eval_questions.yaml
│   ├── savings_baseline.json
│   ├── golden/                     # Golden output files
│   └── memory/                     # Memory fixture data per agent
├── core/                           # Tests for src/atelier/core/
│   ├── capabilities/               # One subdirectory per capability
│   │   ├── prompt_compilation/
│   │   ├── lesson_promotion/
│   │   └── ...
│   ├── service/
│   ├── test_domains.py
│   ├── test_extractor.py
│   └── ...
├── infra/                          # Tests for src/atelier/infra/
│   ├── code_intel/
│   │   ├── astgrep/
│   │   ├── git_history/
│   │   ├── scip/
│   │   └── zoekt/
│   ├── test_store.py
│   ├── test_batch_edit_round_trip.py
│   └── ...
├── gateway/                        # Tests for src/atelier/gateway/
│   ├── test_service_api.py
│   ├── test_security.py
│   └── ...
├── benchmarks/                     # Benchmark/AB tests (marked @ab or @slow)
│   ├── code_intel/
│   └── ...
├── docs/                           # Documentation integrity tests
│   ├── test_readme_no_unmeasured_claims.py
│   └── ...
└── golden/                         # Golden files for snapshot tests
    └── optimization/
```

**Naming:**
- Test files: `test_<feature_or_module>.py`
- Test functions: `test_<behaviour_description>` — descriptive, reads like a sentence
- Test classes: `class Test<Subject>:` — used to group related tests for a single component

## Test Structure

**Dominant style: module-level functions** (not class-based)
Most tests are standalone functions, not grouped in classes. Class-based grouping is used when testing multiple related behaviours of one component.

**Module-level function pattern:**
```python
def test_upsert_and_get_block_roundtrip(store: ContextStore) -> None:
    block = _block()
    store.upsert_block(block)
    fetched = store.get_block(block.id)
    assert fetched is not None
    assert fetched.title == "Title"
    assert (store.blocks_dir / f"{block.id}.md").exists()
```

**Class-based pattern (for importers, adapters):**
```python
class TestClaudeImporterTokens:
    def test_claude_token_fields(self, store: ContextStore, tmp_path: Path) -> None:
        ...
```

**Section separators within test files:**
Long test files use visual divider comments to group related tests:
```python
# ---------------------------------------------------------------------------
# DomainManager: basic loading
# ---------------------------------------------------------------------------
```

**Return type annotations:**
All test functions are annotated with `-> None`.

**`from __future__ import annotations`:**
Present at the top of every test file.

## Mocking

**Framework:** `unittest.mock` (standard library) — `patch` and `MagicMock`

**Pattern — context manager patching:**
```python
from unittest.mock import patch

with patch("atelier.core.service.usage_sync.sync_usage", return_value=True):
    yield
```

**Pattern — `monkeypatch` for env vars:**
```python
def test_something(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
```

**Pattern — patching at the call site (not origin):**
The `_no_ollama` autouse fixture patches `_ollama_module()` — the single gateway function — so all callers (even those with `from ... import`) are blocked:
```python
with patch(
    "atelier.infra.internal_llm.ollama_client._ollama_module",
    side_effect=OllamaUnavailable("ollama blocked in tests"),
):
    yield
```

**`pytest.importorskip` for optional dependencies:**
```python
FastAPITestClient = pytest.importorskip(
    "fastapi.testclient",
    reason="FastAPI API tests require the api extra",
).TestClient
```

**What to Mock:**
- External network calls (LLM APIs, sync endpoints) — always mocked via autouse fixtures
- Local LLM (Ollama) — blocked by autouse `_no_ollama` fixture
- Environment variables — use `monkeypatch.setenv/delenv`

**What NOT to Mock:**
- SQLite storage — real in-process SQLite with `tmp_path` fixtures used instead
- File system — `tmp_path` provides real isolated directories
- Internal business logic — test against real implementations

## Fixtures

**Global autouse fixtures (`tests/conftest.py`):**

`_isolate_workspace_env` *(autouse)*:
Clears all host workspace env vars and sets `ATELIER_ROOT` / `ATELIER_STORE_ROOT` to an isolated `tmp_path`-based directory. Ensures no test ever reads or mutates the developer's `~/.atelier`.

`_no_network_sync` *(autouse)*:
Patches `sync_usage` to return `True` — prevents any test from calling `atelier.beseam.com`.

`_no_ollama` *(autouse)*:
Patches `_ollama_module()` to raise `OllamaUnavailable` — prevents tests from blocking on a local LLM.

**Shared opt-in fixtures:**

`store` fixture:
```python
@pytest.fixture()
def store(tmp_path: Path) -> ContextStore:
    s = ContextStore(tmp_path / "atelier")
    s.init()
    return s
```

`seeded_runtime` fixture:
```python
@pytest.fixture()
def seeded_runtime(tmp_path: Path) -> Iterator[ContextRuntime]:
    # Loads template blocks and rubrics from .lessons/
    ...
    yield rt
```

**Local factory helpers (in-file):**
Private helper functions in test files create model instances:
```python
def _block(bid: str = "b1", domain: str = "coding", title: str = "Title", **kw: object) -> ReasonBlock:
    base: dict[str, Any] = dict(
        id=bid, title=title, domain=domain,
        situation="When doing X.", procedure=["Step one"],
        triggers=["foo"], dead_ends=["never do bar"],
    )
    base.update(kw)
    return ReasonBlock(**base)
```

**Static fixture files:**
Located in `tests/fixtures/` — JSONL, YAML, and JSON files for importer tests, archival recall evaluation, savings baseline, and memory adapters.

## Markers

**`slow`:**
- Applied via `pytestmark = pytest.mark.slow` at module level for entire benchmark files
- Excluded from default `make test` / `make test-fast` runs
- Files in `tests/benchmarks/code_intel/` are all marked slow

**`ab`:**
- Applied via `pytestmark = pytest.mark.ab` for real A/B benchmark tests
- Writes to `~/.atelier/savings_calibration.jsonl`
- Run explicitly with `make bench-ab`

**`skipif` / `skip`:**
- `pytest.mark.skipif(condition, reason=...)` for environment-dependent tests (e.g., Docker not available)
- `pytest.skip("message")` inline for tests that depend on optional artefacts (integration config files)

**`parametrize`:**
```python
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_read_benchmark(fixture: Path) -> None:
    ...
```

## Coverage

**Requirements:** No enforced coverage threshold — `pytest-cov` is available but not gated in CI
**Report types:** Terminal missing lines (`--cov-report=term-missing`) + HTML (`--cov-report=html`) in `htmlcov/`

**View Coverage:**
```bash
make test-cov
# or directly:
uv run pytest --cov=atelier --cov-report=term-missing --cov-report=html
```

## Test Types

**Unit Tests:**
- Scope: Single class or function, isolated with `tmp_path` and autouse fixtures
- Location: `tests/core/`, `tests/infra/`
- Pattern: Test observable behaviours, not implementation internals

**Integration Tests:**
- Scope: Multiple components together (CLI + store, API + store)
- Uses `click.testing.CliRunner` for CLI integration tests
- Uses `FastAPI TestClient` (in-process, no server) for HTTP API tests
- Location: `tests/gateway/`, `tests/core/capabilities/`

**Benchmark / A-B Tests:**
- Scope: Real token savings measurements vs baseline
- Location: `tests/benchmarks/`
- Marked `@pytest.mark.ab` or `@pytest.mark.slow`
- Run separately with `make bench-ab`

**Documentation Tests:**
- Scope: Verify README claims, generated agent context files are up-to-date, no hardcoded values
- Location: `tests/docs/`, `tests/gateway/test_docs.py`, `tests/gateway/test_generated_agent_contexts.py`

**E2E Tests:**
- Not a separate framework — integration tests against the CLI and HTTP API serve this purpose

## Common Patterns

**CLI testing with CliRunner:**
```python
from click.testing import CliRunner
from atelier.gateway.cli import cli

def test_domain_cli_list(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "list"])
    assert result.exit_code == 0, result.output
    assert "swe.general" in result.output
```

**FastAPI testing with TestClient:**
```python
@pytest.fixture()
def app_no_auth(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    return cast("TestClient", FastAPITestClient(create_app(store_root=store.root)))

def test_health_returns_ok(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
```

**Filesystem testing with tmp_path:**
```python
def test_result_envelope_keys(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("hello\n", encoding="utf-8")
    result = apply_batch_edit([...], atomic=True, repo_root=tmp_path)
    assert set(result.keys()) >= {"applied", "failed", "rolled_back"}
```

**Negative / error path testing:**
```python
def test_domain_cli_info_unknown_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "info", "does.not.exist"])
    assert result.exit_code != 0
```

**JSON output verification:**
```python
def test_domain_cli_list_json(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    ids = {item["bundle_id"] for item in payload}
    assert "swe.general" in ids
```

---

*Testing analysis: 2026-05-28*
