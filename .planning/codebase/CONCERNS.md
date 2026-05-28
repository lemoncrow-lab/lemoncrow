# Codebase Concerns

**Analysis Date:** 2026-05-28

---

## Tech Debt

### God-File Monoliths

**Files:**
- `src/atelier/gateway/cli/app.py` — 9,199 lines
- `src/atelier/gateway/adapters/mcp_server.py` — 5,382 lines
- `src/atelier/core/capabilities/code_context/engine.py` — 6,364 lines
- `src/atelier/core/service/api.py` — 6,008 lines

**Issue:** These four files contain the vast majority of business logic with no internal structural subdivision. `mcp_server.py` houses tool handlers, session management, savings accounting, subprocess spawning, and request routing all at the top level. `app.py` holds the entire CLI surface.

**Impact:** Any change risks unintended side-effects; test coverage of individual behaviours is difficult to achieve; onboarding is very slow. Mypy has been entirely disabled for `app.py` (see below).

**Fix approach:** Incrementally extract cohesive groups of tools/commands into dedicated modules (e.g. `mcp_tools/memory.py`, `mcp_tools/context.py`). The `@mcp_tool` decorator already provides the registration seam.

---

### Mypy Completely Disabled for CLI

**Files:** `src/atelier/gateway/cli/app.py` (9,199 lines)

**Issue:** `pyproject.toml` contains:
```toml
[[tool.mypy.overrides]]
module = ["atelier.gateway.cli.app"]
ignore_errors = true
```
The largest file in the project has zero type-checking enforcement.

**Impact:** Regressions in CLI code are invisible to the type checker. Refactoring is risky.

**Fix approach:** Remove `ignore_errors = true`, resolve the resulting type errors incrementally using `# type: ignore[specific-code]` per site so the suppressions are explicit and counted.

---

### Pervasive Module-Level Mutable Global State in MCP Server

**Files:** `src/atelier/gateway/adapters/mcp_server.py`

**Issue:** Twelve+ `global` keyword mutations scattered across the module:
```python
global _current_ledger          # line 213
global _realtime_ctx            # line 221
global _product_session_id      # line 228
global _product_session_started_at  # line 237
global _last_worker_spawn_time  # line 516
global _runtime_cache           # line 533
global _current_ledger, _realtime_ctx, ...  # line 540-542
global _cached_claude_session_id, _cached_mcp_model  # line 646
global _context_budget_recorder  # line 886
global _sampling_seq            # line 1261
global _remote_client           # line 4674
global _client_sampling_supported  # line 5098
```

**Impact:** Any parallel test or future multi-tenant invocation shares process-wide state. The `_reset_runtime_cache_for_testing()` function exists (line 539) as a workaround that must be called manually in tests.

**Fix approach:** Encapsulate per-session state in a `ServerState` dataclass passed explicitly to tool handlers; use the existing `ContextRuntime` object as the container.

---

### Stub SDK — Entire Abstract Client Unimplemented

**Files:** `src/atelier/gateway/sdk/client.py`

**Issue:** The `AtelierClient` abstract base class has 24+ methods that raise `NotImplementedError`:
- Lines 392, 404, 408, 427, 431, 435, 452, 456, 468, 480, 484, 495, 504, 508, 512, 516, 520, 524, 534, 538, 548, 554

These span core operations: `get_context`, `rescue_failure`, `run_rubric_gate`, `record_trace`, `analyze_failures`, plus all memory operations.

**Impact:** Any code path that reaches the abstract client rather than `LocalClient` or `RemoteClient` will crash at runtime with `NotImplementedError`.

**Fix approach:** Enforce the full interface in `LocalClient` (`src/atelier/gateway/sdk/local.py`) and `RemoteClient` (`src/atelier/gateway/sdk/remote.py`), then add a test that instantiates both and calls each method.

---

### Incomplete Session Ingestion — TODOs Left In Place

**Files:**
- `src/atelier/core/service/ingest_session.py` line 64
- `src/atelier/core/service/ingest_session_directory.py` line 67

**Issue:**
```python
# TODO: Store reconstructed ledger events as traces.
```
Both `ingest_session` and `ingest_session_directory` reconstruct the ledger but do not persist the events as Trace records. The function reports `"status": "success"` with an event count but the traces are never written.

**Impact:** Sessions imported via CLI are not searchable via `atelier search` or visible in the context engine, defeating the purpose of ingestion.

**Fix approach:** Call `store.record_trace(trace)` for each reconstructed event after the ledger is built, consistent with the live recording path.

---

### SWE-Bench Agent Runner Permanently Unimplemented

**Files:** `src/benchmarks/swe/agent_runner.py` line 139

**Issue:**
```python
raise NotImplementedError(
    "NotImplementedError until wired by the integration installer; ..."
)
```
The agent runner stub raises unconditionally. `task_runner.py` catches `NotImplementedError` and treats it as a graceful skip.

**Impact:** SWE-Bench integration tests silently skip all runs. Benchmark results are hollow.

**Fix approach:** Wire a concrete runner implementation (claude-code CLI subprocess or the existing `_spawn_subprocess` helper from `mcp_server.py`) before publishing SWE-Bench scores.

---

## Known Bugs / Race Conditions

### Race Condition: cost_history.json Read-Modify-Write

**Files:** `src/atelier/infra/runtime/cost_tracker.py` lines 236–255

**Issue:** `_append_history` performs an unprotected read-modify-write cycle:
```python
history = load_cost_history(self.root)   # read
entry["calls"].append(rec.to_dict())     # modify
save_cost_history(self.root, history)    # write (overwrites)
```
The code itself comments: `"Persist to history immediately so concurrent runs see updates"` but there is no file lock. Concurrent agent processes on the same root will overwrite each other's records.

**Trigger:** Running two agent sessions simultaneously in the same workspace.

**Fix approach:** Use `filelock` or an SQLite-backed store (already used elsewhere) instead of JSON read-modify-write.

---

### Race Condition: JSONL Savings Files Unprotected Append

**Files:** `src/atelier/gateway/adapters/mcp_server.py` lines 560–570, 741–794

**Issue:** `_append_live_savings_event()` and `_append_savings()` open files in `"a"` mode without any filesystem lock. On Linux, concurrent writes to the same JSONL file from multiple MCP server processes produce interleaved or corrupted lines.

**Impact:** Savings analytics can be corrupted under parallel agent invocations. Audit exports (`atelier report`) may produce incorrect totals.

**Fix approach:** Add a `threading.Lock()` per-file path, or replace with SQLite-backed atomic inserts.

---

## Security Considerations

### `shell=True` in Benchmark Runner

**Files:** `src/benchmarks/tool_bench/runner.py` line 131

**Issue:**
```python
r = subprocess.run(command, shell=True, capture_output=True, text=True, ...)
```
`command` is a string constructed from benchmark fixture data. If fixture data is ever user-supplied or sourced from an untrusted repository, this enables shell injection.

**Risk:** Code execution escalation during benchmark runs.

**Current mitigation:** Benchmarks are developer-facing; no untrusted input path exists today.

**Recommendations:** Replace with `shell=False` and a split command list; validate all inputs before running.

---

### Telemetry Captures Lexical Frustration Signals by Default

**Files:**
- `src/atelier/core/service/telemetry/config.py`
- `src/atelier/core/service/telemetry/local_store.py`

**Issue:** `lexical_frustration_enabled: bool = True` is the default. The telemetry system captures "frustration signals" derived from agent/user message content and sends them remotely when `remote_enabled=True` (also the default). First-run opt-in is presented as a banner that auto-acknowledges if `ATELIER_TELEMETRY=0` is not set.

**Risk:** Developers working on proprietary codebases may unknowingly send interaction pattern data.

**Current mitigation:** `ATELIER_TELEMETRY=0` or `atelier telemetry off` disables remote emission.

**Recommendations:** Make `remote_enabled` default `False`; require explicit opt-in rather than opt-out for remote telemetry.

---

### MCP Server Logs Everything at DEBUG Level

**Files:** `src/atelier/gateway/adapters/mcp_server.py` lines 5320–5330

**Issue:**
```python
handler.setLevel(logging.DEBUG)
mcp_logger.setLevel(logging.DEBUG)
```
`_setup_file_logging()` is called unconditionally in `main()`. Every MCP tool invocation, including raw content passed by the agent, is written to `<root>/mcp/mcp.log` at DEBUG verbosity.

**Risk:** Sensitive content (file contents, code diffs, credentials present in agent context) may be persisted unredacted in log files.

**Current mitigation:** The redaction layer (`src/atelier/core/foundation/redaction.py`) runs on memory inputs, but not on all log lines.

**Recommendations:** Default to `INFO` level; gate `DEBUG` behind `ATELIER_DEBUG=1` env var.

---

## Performance Bottlenecks

### Cold-Start Cost: 6,000+ Line Monolith Imports

**Files:** `src/atelier/gateway/adapters/mcp_server.py`

**Issue:** The MCP server imports the entire tool surface (embedders, store, git adapters, telemetry, pricing) at process startup via top-level imports (lines 29–48). Many of these pull in heavy optional dependencies (litellm, tree-sitter, GitPython, networkx).

**Impact:** MCP server startup latency is perceptible (>500ms on modest hardware). Each agent session restart incurs this cost.

**Fix approach:** Move non-essential imports to the lazy-import pattern already used for some handlers (e.g. `from atelier.core.foundation.identity import ...` at call-site). The `@mcp_tool` decorator already defers handler body execution.

---

### Polling Worker Uses `time.sleep` Busy-Wait

**Files:**
- `src/atelier/core/service/worker.py` line 164
- `src/atelier/core/service/ingest_session_directory.py` line 210

**Issue:** The background worker polls the queue with `time.sleep(self._poll_interval)` and the directory watcher sleeps `time.sleep(1)` in a tight loop.

**Impact:** Non-zero CPU consumption even when idle; on battery-constrained laptops this is measurable.

**Fix approach:** Use `inotify`/`watchdog` for file system events, or a condition variable for the worker queue instead of polling.

---

## Fragile Areas

### MCP Server Global State Reset Required in Tests

**Files:**
- `src/atelier/gateway/adapters/mcp_server.py` line 539
- Tests that call `_reset_runtime_cache_for_testing()`

**Why fragile:** The function `_reset_runtime_cache_for_testing()` clears 8 global variables. Any test that creates a new runtime without calling this first will use stale state from a prior test. Missing a call produces subtly wrong behaviour rather than a clear error.

**Safe modification:** Always call `_reset_runtime_cache_for_testing()` in test fixtures; add an autouse fixture in `tests/conftest.py`.

**Test coverage:** Partial — gateway tests exist but the reset requirement is implicit.

---

### ContextStore / SQLite Store Has No Write Locking

**Files:** `src/atelier/core/foundation/store.py`

**Issue:** `ContextStore` uses Python's `sqlite3` module with `check_same_thread=False` implied (connection per call via `_connect()`). Multiple concurrent writers (e.g. background worker + MCP server) can produce `database is locked` SQLite errors on WAL-mode boundary conditions.

**Why fragile:** SQLite's own WAL handles *reads* safely, but concurrent Python processes opening new connections to the same DB file without coordinating can produce transient lock errors. The `batch_mode()` context manager helps within a single process but does not coordinate across processes.

**Safe modification:** Ensure all multi-process write paths use the `Worker` service as the single writer, or migrate to PostgreSQL for concurrent deployments.

---

### `pygit2` Pinned to Exact Version

**Files:** `pyproject.toml` line 25

**Issue:**
```toml
"pygit2==1.19.2",
```
`pygit2` wraps `libgit2` and has exact version pinning. Unlike other deps which use `>=` constraints, this will break on any system where `libgit2` version skew prevents `1.19.2` from installing.

**Impact:** Fresh installs on newer OS releases (e.g. Ubuntu 24.04 LTS with newer libgit2) fail silently or produce resolver errors.

**Fix approach:** Use `>=1.19.2,<2` or add a `try/except ImportError` fallback path (a `require_pygit2` guard already exists at `src/atelier/infra/code_intel/git_history/__init__.py`; ensure all callers use it).

---

### `tree-sitter-languages` Unavailable on Python 3.13

**Files:** `pyproject.toml` lines 55, 68

**Issue:**
```toml
"tree-sitter-languages>=1.10; python_version < '3.13'"
```
This legacy package is conditionally excluded on Python 3.13+. Features that depend on it (repo-map, parsers extras) silently degrade on 3.13.

**Impact:** `atelier index` and repo-map features may produce empty or incorrect results on 3.13 without a visible error.

**Fix approach:** Add a runtime check that logs a warning when the language pack is unavailable and features are requested; migrate to `tree-sitter-language-pack` (already a core dependency) for all language loading.

---

## Test Coverage Gaps

### 9,199-Line CLI (`app.py`) Has No Mypy and Thin Test Coverage

**What's not tested:** Most `atelier` CLI sub-commands beyond smoke tests. No tests for `global_import`, `consolidation`, `proof`, `sql`, `team` subcommands.

**Files:** `src/atelier/gateway/cli/app.py`, `tests/gateway/test_cli.py`, `tests/gateway/test_cli_coverage.py`

**Risk:** Silent regressions in CLI commands used by end-users.

**Priority:** High

---

### `mcp_server.py` Tool Handlers Lack Isolated Unit Tests

**What's not tested:** Individual MCP tool handlers (`tool_get_context`, `tool_memory`, `tool_trace`, `tool_verify`, `tool_route`, `tool_compact`) in isolation. Existing gateway tests (`tests/gateway/`) test the HTTP/MCP surface but not internal handler logic.

**Files:** `src/atelier/gateway/adapters/mcp_server.py`, `tests/gateway/test_mcp_memory_tools.py`, `tests/gateway/test_context_mcp_handler.py`

**Risk:** Global state mutation bugs go undetected; regression requires full integration run.

**Priority:** High

---

### SWE-Bench Benchmark Tests Use `xfail` for Missing Fixtures

**What's not tested:** `tests/benchmarks/test_read_ab_real.py` calls `pytest.xfail(f"fixture missing: {fixture}")` when fixture files are absent. All benchmark tests that depend on real replay fixtures are conditionally silenced.

**Files:** `tests/benchmarks/test_read_ab_real.py` lines 124, 216, 309, 357, 414, 461

**Risk:** Benchmark regressions go undetected when fixtures are not committed; CI green does not mean benchmarks pass.

**Priority:** Medium

---

### `ingest_session` and `ingest_session_directory` — TODO Code Untested End-to-End

**What's not tested:** The full ingestion pipeline from file → Trace record. The TODO at line 64 of `ingest_session.py` means traces are never stored; tests that assert "ingestion works" can pass even though the core purpose is unimplemented.

**Files:** `src/atelier/core/service/ingest_session.py`, `src/atelier/core/service/ingest_session_directory.py`

**Risk:** Users running `atelier import` believe their sessions are indexed when they are not.

**Priority:** High

---

## Dependencies at Risk

### `pygit2==1.19.2` — Exact Pin

**Risk:** libgit2 ABI changes on newer OS releases will block installation.

**Impact:** Git history features (blame, rename tracking, commit walk) become unavailable.

**Migration plan:** Use `>=1.19.2` with graceful `require_pygit2()` guard; or vendor a wheels mirror for supported platforms.

---

### `litellm>=1.83.14` — Rapidly Changing Upstream

**Risk:** litellm releases multiple times per week with frequent breaking changes to provider adapters. A minor version bump has historically broken routing and model-name resolution.

**Impact:** `tool_route` and cost estimation may silently use wrong pricing tables after an upgrade.

**Migration plan:** Pin to a specific minor version range (e.g. `>=1.83,<1.90`); add a CI test that validates the pricing table against known model names.

---

## Missing Critical Features

### Session Ingestion Does Not Persist Traces

**Problem:** `ingest_session` and `ingest_session_directory` reconstruct the ledger but the resulting traces are never written to the `ContextStore`.

**Blocks:** `atelier search` over imported sessions; context reuse across host platforms; cross-session analytics.

### SDK Abstract Client Has No Default Implementations

**Problem:** `AtelierClient` base class has 24+ unimplemented methods.

**Blocks:** Third-party integrations that rely on the SDK contract; plugin authors who subclass `AtelierClient`.

---

*Concerns audit: 2026-05-28*
