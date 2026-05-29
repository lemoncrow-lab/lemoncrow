# Phase 23: Silent Exception Audit - Pattern Map

**Mapped:** 2026-05-29
**Files analyzed:** 15 source files (28 silent `pass` sites + 3 in-scope `continue` sites) + `pyproject.toml`
**Analogs found:** 15 / 15 (all in-repo; same-module observable-handling patterns exist for every site)

> **Nature of this phase:** No new files. This is a surgical *modify-in-place* pass on
> existing broad-except handlers. "Analogs" here are existing handlers in the **same or
> nearby module** that already do the right thing — observable log with `exc_info=True`,
> a narrowed exception tuple, or a control-flow fallback. The planner copies these exact
> shapes into the in-scope silent sites. Scope is strictly the 28 `pass` sites (QBL-EXC-01/02)
> plus the 3 `continue` sites that block clearing the 8 removable files' `BLE001` ignore.

## File Classification

Classified by module tier (role) and the failure-handling data flow at the silent site.
**Fix class:** B = best-effort (keep suppression + observable log), N = narrow type + log,
C = control-flow fallback (narrow to expected lookup/SDK exception).

| Modified File | Role (tier) | Data Flow | Sites (class) | Closest Analog | Match | Ignore removable? |
|---------------|-------------|-----------|---------------|----------------|-------|-------------------|
| `core/environment.py` | config resolver | file-I/O fallback | 1 (N) | `gateway/hosts/session_parsers/cline.py:50-62` | exact | **YES** |
| `gateway/adapters/mcp_server.py` | MCP adapter | request-response / file-I/O / telemetry | 9 (4 N, 5 B) | same file `_write_smart_state` L819-825 | exact (in-file) | NO (keeps BLE001+T201) |
| `gateway/cli/app.py` | CLI | subprocess / file-I/O / aggregation | 4 (1 N, 3 B) | same file L5079; `_record_context_budget` L4957 | exact (in-file) | NO |
| `core/capabilities/code_context/engine.py` | capability/service | event-driven (lineage walk) | 1 (B) | `core/capabilities/context_compression/sleeptime.py:45-49` | role-match | NO |
| `infra/code_intel/git_history/adapter.py` | infra adapter | transform (rename heuristic) | 1 (B) | `gateway/hosts/session_parsers/_common.py:834-835` | role-match | NO |
| `infra/memory_bridges/letta_adapter.py` | infra adapter | request-response (SDK lookup) | 2 (C) | `core/service/api.py:4708` warn+exc_info | role-match | NO |
| `sdk/anthropic_tools.py` | SDK middleware | event-driven callback | 1 (B) | `gateway/adapters/mcp_server.py:4957` | role-match | **YES** |
| `sdk/gemini_adk.py` | SDK middleware | event-driven callback | 1 (B) | `gateway/adapters/mcp_server.py:4957` | role-match | **YES** |
| `sdk/langchain_middleware.py` | SDK middleware | event-driven callback | 2 (B) | `gateway/adapters/mcp_server.py:4957` | role-match | **YES** |
| `benchmarks/swe/compact_bench.py` | benchmark parser | transform / parse-loop | 1 pass (N) + 1 continue | `gateway/hosts/session_parsers/cline.py:57` | exact | **YES** |
| `benchmarks/swe/routing_bench.py` | benchmark parser | transform / parse-loop | 1 pass (N) + 1 continue | `core/capabilities/tool_supervision/post_edit_hooks.py:127` | exact | **YES** |
| `benchmarks/swe/routing_quality_bench.py` | benchmark parser | transform / parse-loop | 1 pass (N) + 1 continue | `core/service/api.py:4012` | exact | **YES** |
| `benchmarks/swe/routing_replay_bench.py` | benchmark parser | transform / parse-loop | 1 (N) | `core/capabilities/tool_supervision/post_edit_hooks.py:127` | exact | NO |
| `benchmarks/tool_bench/report.py` | benchmark reporter | transform (regex parse) | 1 (B) | `gateway/hosts/session_parsers/_common.py:834-835` | role-match | NO (keeps T201) |
| `benchmarks/tool_bench/runner.py` | benchmark runner | parse (MCP stdout JSONL) | 1 (N) | `gateway/hosts/session_parsers/codex.py:571` | exact | **YES** |

## Pattern Assignments

### Group A — Best-effort sites (class B): keep suppression, add observable log

**Sites:** mcp_server.py 770/795/1183/1469; cli/app.py 5931/5966/6052; engine.py 6433;
git_history/adapter.py 319; anthropic_tools.py 133; gemini_adk.py 105;
langchain_middleware.py 172/195; report.py 289.

**Canonical analog (in-repo, in same file as 4 of the sites):**
`src/atelier/gateway/adapters/mcp_server.py:819-825`
```python
def _write_smart_state(state: dict[str, Any]) -> None:
    try:
        path = _smart_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("Suppressed exception while writing smart_state", exc_info=True)
```

**Best-effort with rationale comment (telemetry analog):**
`src/atelier/core/service/telemetry/emit.py:52-60`
```python
        try:
            from atelier.core.service.telemetry.exporters.otel import shutdown_otel
            shutdown_otel()
        except Exception:
            logger.warning(
                "Suppressed exception at emit.py:48",
                exc_info=True,
            )
```

**Per-record debug (loop body) analog:**
`src/atelier/gateway/hosts/session_parsers/_common.py:834-835`
```python
        except Exception:
            logger.debug("snapshot_edited_files: failed to save %s", fpath, exc_info=True)
```

**Apply to each class-B site:**
- Keep the broad `except Exception:` body's suppression semantics (do NOT re-raise — these
  are best-effort side effects; surfacing would break tool calls / host callbacks, see
  RESEARCH Pitfall 3 + Assumption A2).
- Replace silent `pass` with `logger.debug("<what failed>", exc_info=True)` (use `warning`
  only where the failure degrades user-visible function; research recommends `debug` for
  these). Add a one-line `# why best-effort` comment if one isn't already present.
- **Logging level note:** `engine.py`, `git_history/adapter.py`, `anthropic_tools.py`,
  `gemini_adk.py`, `langchain_middleware.py`, `report.py` have **no logger** — see
  Shared Pattern: Module Logger. In `mcp_server.py` reuse `_log` / `logger`; in `cli/app.py`
  reuse `logger`.
- **BLE001 stays** for class-B sites whose file keeps its ignore (the log does NOT clear
  BLE001 — RESEARCH Pitfall 1). For the SDK files (anthropic/gemini/langchain), they are
  ALSO removable — so either narrow OR add `# noqa: BLE001 — best-effort cache/token capture`.

---

### Group B — Narrow + log sites (class N)

**Sites:** environment.py 161; mcp_server.py 634/665/690/712/5443; cli/app.py 1215;
compact_bench.py 178; routing_bench.py 248; routing_quality_bench.py 351;
routing_replay_bench.py 223; runner.py 93.

**File-write narrow analog:** narrow to `(OSError,)` —
`mcp_server.py` 634 writes a sidecar JSON file.

**File-read JSON narrow analog (exact shape to copy):**
`src/atelier/gateway/hosts/session_parsers/cline.py:53-62`
```python
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(entries, list):
            return {str(e["id"]): e for e in entries if "id" in e}
    except (OSError, json.JSONDecodeError, KeyError):
        logger.warning(
            "Suppressed exception at cline.py:50",
            exc_info=True,
        )
    return {}
```
→ apply to mcp_server.py 665/690/712 (`(OSError, json.JSONDecodeError)`).

**Subprocess narrow analog (in cli/app.py itself):**
`src/atelier/gateway/cli/app.py:5079`
```python
    except (OSError, subprocess.SubprocessError) as exc:
        out = f"(rg failed: {exc})"
```
→ apply to cli/app.py 1215 (`_detect_git_root`) and mcp_server.py 5443
(`git rev-parse` → `setdefault(cwd)`): narrow to `(OSError, subprocess.SubprocessError)`.

**TOML config narrow:** environment.py 161 → narrow to
`(tomllib.TOMLDecodeError, OSError, ValueError)`, keep the defaults fallback, log at
`warning(..., exc_info=True)`.
Current code to replace (`src/atelier/core/environment.py:155-163`):
```python
            try:
                data = tomllib.loads(config_path.read_text(encoding="utf-8"))
                ...
            except Exception:
                # Keep runtime robust; invalid config falls back to defaults.
                pass
```

**Parse-loop narrow analog (benchmarks):**
`src/atelier/core/capabilities/tool_supervision/post_edit_hooks.py:124-128`
```python
def _parse_ruff_json(stdout: str, source: str = "ruff") -> list[DiagnosticItem]:
    try:
        items = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return []
```
and `gateway/hosts/session_parsers/codex.py:571` → `except (json.JSONDecodeError, TypeError):`.
→ For transcript/event parse loops (compact_bench 178, routing_bench 248,
routing_quality_bench 351, routing_replay_bench 223, runner.py 93) narrow to the realistic
tuple `(json.JSONDecodeError, KeyError, ValueError, TypeError)` (add `OSError` if the body
reads a file). **Do NOT over-narrow to `json.JSONDecodeError` alone** — RESEARCH Pitfall 4:
a `KeyError`/`TypeError` would then crash a previously-resilient run.

**Apply to each class-N site:** replace `except Exception:` with the narrowed tuple, keep
the fallback return/continue, and add `logger.debug/.warning(..., exc_info=True)` where a
logger is available. Narrowing is what actually clears `BLE001`.

---

### Group C — Control-flow fallback sites (class C)

**Sites:** letta_adapter.py 93 (`upsert_block`: block-missing → create+attach),
letta_adapter.py 121 (`get_block`: new-SDK retrieve → flat `get_block`).

**Analog (warn + exc_info, surfacing real failures):**
`src/atelier/core/service/api.py:4708`
```python
            logger.warning("Failed to load MCP tool status: %s", exc, exc_info=True)
```

**Apply:** Narrow to the Letta "not found" SDK exception **if the installed letta-client
exposes one** (executor verifies against the SDK — RESEARCH Assumption A3); the
`except` is a deliberate control-flow branch to the fallback path, so keep the fallback.
If no specific exception type exists, keep broad + `logger.debug(..., exc_info=True)` and
leave the file's `BLE001` ignore (17 other broad handlers remain anyway — not removable).
Covered by `tests/infra/test_letta_adapter_fallback.py`.

---

## Shared Patterns

### Module Logger (PREREQUISITE for 13 of 15 files)
**Source convention:** `src/atelier/gateway/hosts/session_parsers/cline.py:36`
```python
import logging
logger = logging.getLogger(__name__)
```
**Apply to (add at module top, after imports — these files have NO logger today):**
`core/environment.py`, `core/capabilities/code_context/engine.py`,
`infra/code_intel/git_history/adapter.py`, `infra/memory_bridges/letta_adapter.py`,
`sdk/anthropic_tools.py`, `sdk/gemini_adk.py`, `sdk/langchain_middleware.py`,
`benchmarks/swe/compact_bench.py`, `benchmarks/swe/routing_bench.py`,
`benchmarks/swe/routing_quality_bench.py`, `benchmarks/swe/routing_replay_bench.py`,
`benchmarks/tool_bench/report.py`, `benchmarks/tool_bench/runner.py`.
**Already have a logger (reuse, do NOT add):**
- `gateway/adapters/mcp_server.py` — `logger` (L50) and `_log = logging.getLogger("atelier.mcp")` (L370). Prefer `_log` for module-level function sites.
- `gateway/cli/app.py` — `logger = logging.getLogger(__name__)` (L65).

### NEVER use print() inside mcp_server.py
**Why:** stdout is the MCP JSON-RPC framing channel; a `print()` corrupts it (RESEARCH
Pitfall 2). All observability must go through `logging` (stderr). Guarded by
`tests/gateway/test_mcp_stdio_smoke.py`. The `T201` token stays in mcp_server.py's ignore
(Phase 24 owns print burn-down).

### BLE001 ignore removal (pyproject.toml)
**Source:** `pyproject.toml` lines 131-214 (`[tool.ruff.lint.per-file-ignores]`, BLE001-only block).
**Mechanic:** A log does NOT clear BLE001 — only narrowing the handler or an inline
`# noqa: BLE001 — <reason>` does. Remove an ignore line ONLY when the file has zero
remaining un-annotated broad handlers.
**Delete these 8 lines after their sites are narrowed (prefer narrow over `# noqa`):**
```
"src/atelier/core/environment.py" = ["BLE001"]
"src/atelier/sdk/anthropic_tools.py" = ["BLE001"]
"src/atelier/sdk/gemini_adk.py" = ["BLE001"]
"src/atelier/sdk/langchain_middleware.py" = ["BLE001"]
"src/benchmarks/tool_bench/runner.py" = ["BLE001"]
"src/benchmarks/swe/compact_bench.py" = ["BLE001"]
"src/benchmarks/swe/routing_bench.py" = ["BLE001"]
"src/benchmarks/swe/routing_quality_bench.py" = ["BLE001"]
```
For `compact_bench`/`routing_bench`/`routing_quality_bench` the in-scope `continue` site
must ALSO be narrowed or the ignore can't be removed (each file has 2 broad handlers).

### Optional regression test
**Candidate:** `environment.py:161` invalid config → assert `logger.warning` emitted via
`caplog`. Extend `tests/infra/test_memory_backend_selection.py`. (M2 "add if cheap".)

## No Analog Found

None. Every site has an in-repo observable-handling analog (most in the same module).

## Validation Commands

```bash
# Lint must stay green after EACH ignore-line deletion (catches missed handlers):
uv run ruff check src --select BLE001
# Required MCP/tool-handler coverage (QBL-EXC-04):
uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_stdio_smoke.py -q
# SDK + memory + environment touched modules:
uv run pytest tests/gateway/test_sdk_middleware.py tests/infra/test_letta_adapter_fallback.py tests/infra/test_memory_backend_selection.py -q
# Re-run enumeration; confirm pass-count drops to 0 (or each survivor has # noqa):
grep -rn -A1 "except Exception" src --include='*.py' | grep -B1 "pass" | grep -c "except Exception"
# Phase gate:
make lint && make typecheck && make test
```

## Metadata

**Analog search scope:** `src/atelier/**` (gateway, core, infra, sdk), `src/benchmarks/**`.
**Files scanned:** 44 `exc_info=True` usages, 40+ `logging.getLogger` declarations,
narrowed-tuple examples in session_parsers, post_edit_hooks, api.py, cli/app.py.
**Pattern extraction date:** 2026-05-29
