# Phase 23: Silent Exception Audit - Research

**Researched:** 2026-05-29
**Domain:** Python exception-handling hygiene / observability burn-down (Ruff BLE001)
**Confidence:** HIGH (all findings derived from direct AST + lint analysis of the live tree)

## Summary

Phase 23 makes silent broad-except suppressions observable and shrinks the `BLE001`
per-file-ignore ledger that Phase 22 introduced. A fresh AST scan of `src/**/*.py`
(robust to formatting, unlike the M2 grep) finds **80 silent broad-except handlers**:
**28 `except Exception: pass`** (exactly matching M2's count) plus **52 `except
Exception: continue`** sites. No bare `except:` and no silent `break`/`...` sites exist.
The 28 `pass` sites span **15 files**; all 15 are already in the `BLE001`
per-file-ignores list.

The decisive planning insight is about **what it takes to remove a file from the
`BLE001` ignore list**. Ruff's `BLE001` fires on *every* `except Exception`/`except
BaseException` handler **regardless of body** — adding `logger.debug(..., exc_info=True)`
makes the failure observable but does **not** clear the lint violation. A file can only
drop its per-file-ignore when **zero** un-annotated broad-excepts remain, i.e. every
broad handler in the file is either **narrowed** to specific types or carries an inline
`# noqa: BLE001` justification. Most touched files contain many *non-silent* broad
handlers beyond the in-scope silent ones (mcp_server.py has 28 broad handlers total,
cli/app.py 20, letta_adapter.py 19, engine.py 12). Clearing those would be a broad
refactor, which the phase constraints forbid. Therefore only **8 of the 15 files**
can have their ignore removed within a surgical pass; the other 7 get their silent sites
made observable but **keep their ignore** (documented, not a failure).

**Primary recommendation:** Treat the 28 `pass` sites as required scope (QBL-EXC-01/02).
Fix each as best-effort-log-with-rationale or narrow-and-surface. For the 8 files whose
*only* broad handlers are in-scope silent sites, **narrow** the exception types so
`BLE001` no longer fires and remove the per-file-ignore (QBL-EXC-03). For the other 7
files, make the silent sites observable but leave the ignore in place and document why.
All logging must use the `logging` module (stderr) — never `print()` — to protect MCP
stdio framing.

## Project Constraints (from phase prompt / copilot conventions)

- Python commands MUST use `uv run` (e.g. `uv run pytest`, `uv run ruff`).
- **No broad refactors** — Phase 23 is a surgical audit/fix pass.
- Only touch broad `except Exception:` blocks whose body is a silent `pass` (or
  equivalent silent suppression — `continue`/`break` — directly in scope).
- Research must NOT modify source code (executor applies fixes later).
- Pre-existing unrelated repo validation blockers are **baseline**, not Phase 23 work.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| QBL-EXC-01 | Fresh enumeration of `except Exception: pass` sites captured before fixes | Fresh AST inventory below — 28 `pass` sites across 15 files (+52 `continue`) |
| QBL-EXC-02 | Every silent broad-except removed, narrowed, re-raised, or explicitly logged with rationale | Per-site classification + recommended-fix table |
| QBL-EXC-03 | Fixed files removed from BLE001 per-file ignores | BLE001 shrink plan: 8 files fully removable, 7 documented as ignore-retained |
| QBL-EXC-04 | MCP/tool-handler focused tests cover touched gateway surfaces | Validation plan: `test_mcp_tool_handlers.py`, `test_p0_mcp_surfaces.py`, `test_mcp_stdio_smoke.py` |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| MCP tool-call error handling | Gateway / MCP adapter | — | `mcp_server.py` owns the stdio JSON-RPC surface; suppression here must stay quiet on stdout |
| Best-effort telemetry / savings emit | Gateway + Core service | Infra storage | Failures must log but never break the caller |
| SDK middleware callbacks (cache/token capture) | SDK adapters | — | Must never break the host agent's model call; pure observability |
| Session/transcript parsing | Gateway host parsers / Benchmarks | — | Per-record skip loops; best-effort by design |
| Config / git-root resolution | Core environment + CLI | — | Fail-open to defaults; observable fallback |

## Fresh Inventory — 28 Silent `except Exception: pass` Sites

Enumerated via AST (`ast.ExceptHandler` with broad type and single-statement `Pass`
body). Counts independently confirmed against the M2 grep (`grep -rn -A1 "except
Exception" src --include='*.py' | grep -B1 "pass"` → 28).

| # | File:Line | Function / Context | Behavior at site |
|---|-----------|--------------------|------------------|
| 1 | `core/capabilities/code_context/engine.py:6433` | `CodeContextEngine._lineage_bootstrap_worker` | Lineage walk; already commented "fail-open — lineage additive" |
| 2 | `core/environment.py:161` | `resolve_memory_backend` | Invalid `config.toml` → fall back to defaults (commented) |
| 3 | `gateway/adapters/mcp_server.py:634` | `_register_mcp_session` | Write session sidecar JSON file |
| 4 | `gateway/adapters/mcp_server.py:665` | `_get_claude_session_id` | Read session file → fallback to product session id |
| 5 | `gateway/adapters/mcp_server.py:690` | `_get_mcp_model` | Read model from session file (fallback) |
| 6 | `gateway/adapters/mcp_server.py:712` | `_get_host_session_sidecar_path` | Read sid from session file (fallback) |
| 7 | `gateway/adapters/mcp_server.py:770` | `_append_savings` | Append savings JSONL ledger |
| 8 | `gateway/adapters/mcp_server.py:795` | `_append_savings` | Append per-session context-savings JSONL |
| 9 | `gateway/adapters/mcp_server.py:1183` | `tool_get_context` | Prefix-cache planning; commented "never break tool_context" |
| 10 | `gateway/adapters/mcp_server.py:1469` | `tool_route` | Model recommendation best-effort |
| 11 | `gateway/adapters/mcp_server.py:5443` | `main` | `git rev-parse` for workspace root → `setdefault(cwd)` |
| 12 | `gateway/cli/app.py:1215` | `_detect_git_root` | `git rev-parse`; returns `None` on failure |
| 13 | `gateway/cli/app.py:5931` | `_render_dashboard_impl` | Cost/savings aggregation per row |
| 14 | `gateway/cli/app.py:5966` | `_render_dashboard_impl` | SQLite trace read for dashboard |
| 15 | `gateway/cli/app.py:6052` | `_render_dashboard_impl` | Parse run JSON files for dashboard |
| 16 | `infra/code_intel/git_history/adapter.py:319` | `DeletedHistorySearchAdapter._resolved_rename_target` | Rename-target heuristic; caches `None` on failure |
| 17 | `infra/memory_bridges/letta_adapter.py:93` | `LettaAdapter.upsert_block` | Block may not exist → create+attach (commented; control-flow-ish) |
| 18 | `infra/memory_bridges/letta_adapter.py:121` | `LettaAdapter.get_block` | New-SDK retrieve → fallback to flat `get_block` |
| 19 | `sdk/anthropic_tools.py:133` | `make_atelier_tools.dispatch` | Prefix-cache planning in tool dispatch |
| 20 | `sdk/gemini_adk.py:105` | `GeminiADKMiddleware.on_model_end` | Prefix-cache planning in model-end callback |
| 21 | `sdk/langchain_middleware.py:172` | `LangChainMiddleware.on_llm_end` | Token-usage extraction from LLM output |
| 22 | `sdk/langchain_middleware.py:195` | `LangChainMiddleware.on_llm_end` | Prefix-cache planning |
| 23 | `benchmarks/swe/compact_bench.py:178` | `_parse_session` | Transcript turn parse |
| 24 | `benchmarks/swe/routing_bench.py:248` | `_parse_session_routing` | Routing event parse |
| 25 | `benchmarks/swe/routing_quality_bench.py:351` | `_parse_events` | Event parse |
| 26 | `benchmarks/swe/routing_replay_bench.py:223` | `_parse_tool_response` | JSON-from-text parse attempt loop |
| 27 | `benchmarks/tool_bench/report.py:289` | `print_enforcement_gap` | Front-matter regex parse |
| 28 | `benchmarks/tool_bench/runner.py:93` | `_mcp_call` | Parse MCP stdout JSONL line |

**Equivalent silent suppressions (`continue`) — 52 sites, secondary scope.** These are
mostly per-record skip-bad-record loops in session parsers
(`gateway/hosts/session_parsers/_session_parser.py` alone has 19) and code-intel stores.
They are legitimately best-effort by design. Only the `continue` sites that co-reside in
the 8 fully-removable files (compact_bench, routing_bench, routing_quality_bench — 1 each)
**must** be handled to clear those files' ignores; the rest are out of the surgical scope
and should be left as-is (optionally add a `logger.debug(exc_info=True)` if cheap).

## Classification & Recommended Fix

Classification key: **B** = intentional best-effort (keep suppression, add observable
log + rationale comment); **N** = narrow exception type (and surface/log real failures);
**C** = control-flow fallback (narrow to the expected SDK/lookup exception).

| # | Site | Class | Recommended fix |
|---|------|-------|-----------------|
| 1 | engine.py:6433 | B | Already commented; add `logger.debug("lineage bootstrap failed", exc_info=True)` (needs new module logger) |
| 2 | environment.py:161 | N | Narrow to `(tomllib.TOMLDecodeError, OSError, ValueError)`; keep fallback; add `logger.warning(exc_info=True)` |
| 3 | mcp_server.py:634 | N | Narrow to `(OSError,)` (file write); `_log.debug(exc_info=True)` |
| 4 | mcp_server.py:665 | N | Narrow to `(OSError, json.JSONDecodeError)`; `_log.debug(exc_info=True)` |
| 5 | mcp_server.py:690 | N | Narrow to `(OSError, json.JSONDecodeError)`; `_log.debug(exc_info=True)` |
| 6 | mcp_server.py:712 | N | Narrow to `(OSError, json.JSONDecodeError)`; `_log.debug(exc_info=True)` |
| 7 | mcp_server.py:770 | B | Best-effort telemetry; keep, `_log.debug("savings append failed", exc_info=True)` |
| 8 | mcp_server.py:795 | B | Best-effort telemetry; keep, `_log.debug(exc_info=True)` |
| 9 | mcp_server.py:1183 | B | Keep (commented); `_log.debug("prefix planning failed", exc_info=True)` |
| 10 | mcp_server.py:1469 | B | Keep; `_log.debug("model recommendation failed", exc_info=True)` |
| 11 | mcp_server.py:5443 | N | Narrow to `(OSError, subprocess.SubprocessError)`; `_log.debug(exc_info=True)` |
| 12 | cli/app.py:1215 | N | Narrow to `(OSError, subprocess.SubprocessError)`; `logger.debug(exc_info=True)` |
| 13 | cli/app.py:5931 | B | Keep; `logger.debug("dashboard cost agg failed", exc_info=True)` |
| 14 | cli/app.py:5966 | B | Keep; `logger.debug(exc_info=True)` |
| 15 | cli/app.py:6052 | B | Keep; `logger.debug(exc_info=True)` |
| 16 | git_history/adapter.py:319 | B | Keep (heuristic); add module logger + `logger.debug(exc_info=True)` |
| 17 | letta_adapter.py:93 | C | Narrow to the Letta "not found" exception if available, else keep+log; comment is control-flow |
| 18 | letta_adapter.py:121 | C | Narrow / keep+log; fallback to flat get_block |
| 19 | anthropic_tools.py:133 | B | Best-effort cache planning; add module logger + `logger.debug(exc_info=True)` |
| 20 | gemini_adk.py:105 | B | Same; must never break host model call |
| 21 | langchain_middleware.py:172 | B | Token extraction best-effort; add logger + debug log |
| 22 | langchain_middleware.py:195 | B | Cache planning best-effort; debug log |
| 23 | compact_bench.py:178 | N | Narrow to `(json.JSONDecodeError, KeyError, ValueError, TypeError)` |
| 24 | routing_bench.py:248 | N | Narrow to parse exceptions |
| 25 | routing_quality_bench.py:351 | N | Narrow to parse exceptions |
| 26 | routing_replay_bench.py:223 | N | Narrow to `(json.JSONDecodeError, ValueError)` |
| 27 | report.py:289 | B | Keep (regex parse best-effort); add logger + debug log |
| 28 | runner.py:93 | N | Narrow to `(json.JSONDecodeError, ValueError)` |

> **Logger prerequisite (critical):** 13 of the 15 touched files have **no logger at
> all** (verified — zero `logging`/`logger` references). Only `mcp_server.py` (`logger`,
> `_log = logging.getLogger("atelier.mcp")`) and `cli/app.py` (`logger =
> logging.getLogger(__name__)`) already have one. Every "add log" fix in the other 13
> files first requires adding `import logging` + `logger = logging.getLogger(__name__)`
> at module level. The planner should make "add module logger" an explicit per-file
> sub-step, not assume one exists.

## BLE001 Ignore Shrink Plan

**Mechanics:** `BLE001` flags any `except Exception`/`except BaseException` handler
*regardless of body*. A `logger.debug(...)` does **not** clear it. To remove a file from
`[tool.ruff.lint.per-file-ignores]`, the file must have **zero** remaining un-annotated
broad handlers — every broad handler must be **narrowed** (no longer `except Exception`)
or carry an inline `# noqa: BLE001 — <reason>`. Files also carrying `T201` (print debt)
keep that token in the ignore (Phase 24 owns print burn-down); only `BLE001` is dropped.

Broad-handler census per touched file (AST-verified):

| File | Broad handlers (total) | In-scope `pass` | In-scope `continue` | Other broad | BLE001 ignore removable in Phase 23? |
|------|------------------------|-----------------|---------------------|-------------|---------------------------------------|
| `core/environment.py` | 1 | 1 | 0 | 0 | **YES** — narrow the 1 site → drop ignore |
| `sdk/anthropic_tools.py` | 1 | 1 | 0 | 0 | **YES** (narrow or `# noqa` the 1 site) |
| `sdk/gemini_adk.py` | 1 | 1 | 0 | 0 | **YES** |
| `sdk/langchain_middleware.py` | 2 | 2 | 0 | 0 | **YES** |
| `benchmarks/tool_bench/runner.py` | 1 | 1 | 0 | 0 | **YES** |
| `benchmarks/swe/compact_bench.py` | 2 | 1 | 1 | 0 | **YES** (must also fix the `continue`) |
| `benchmarks/swe/routing_bench.py` | 2 | 1 | 1 | 0 | **YES** (must also fix the `continue`) |
| `benchmarks/swe/routing_quality_bench.py` | 2 | 1 | 1 | 0 | **YES** (must also fix the `continue`) |
| `core/capabilities/code_context/engine.py` | 12 | 1 | 3 | 8 | NO — 8 non-silent broad handlers remain |
| `gateway/adapters/mcp_server.py` | 28 | 9 | 1 | 18 | NO — 18 remain (also keeps `T201`) |
| `gateway/cli/app.py` | 20 | 4 | 1 | 15 | NO — 15 remain |
| `infra/memory_bridges/letta_adapter.py` | 19 | 2 | 0 | 17 | NO — 17 remain |
| `infra/code_intel/git_history/adapter.py` | 3 | 1 | 0 | 2 | NO — 2 remain |
| `benchmarks/swe/routing_replay_bench.py` | 4 | 1 | 1 | 2 | NO — 2 remain |
| `benchmarks/tool_bench/report.py` | 4 | 1 | 0 | 3 | NO — 3 remain (also keeps `T201`) |

**Ignore lines to remove after fixes (8 files, all in the "BLE001 only" block, lines
131–214 of `pyproject.toml`):**
- `"src/atelier/core/environment.py" = ["BLE001"]` → delete
- `"src/atelier/sdk/anthropic_tools.py" = ["BLE001"]` → delete
- `"src/atelier/sdk/gemini_adk.py" = ["BLE001"]` → delete
- `"src/atelier/sdk/langchain_middleware.py" = ["BLE001"]` → delete
- `"src/benchmarks/tool_bench/runner.py" = ["BLE001"]` → delete
- `"src/benchmarks/swe/compact_bench.py" = ["BLE001"]` → delete
- `"src/benchmarks/swe/routing_bench.py" = ["BLE001"]` → delete
- `"src/benchmarks/swe/routing_quality_bench.py" = ["BLE001"]` → delete

> **Important:** For the 8 removable files, prefer **narrowing** the exception type to
> genuinely clear `BLE001` (no `# noqa` needed). An inline `# noqa: BLE001` also works but
> leaves the broad handler in place. After deleting each ignore line, `uv run ruff check
> src --select BLE001` must stay green — if narrowing missed a handler, ruff will fail.

**The 7 retained-ignore files** (engine, mcp_server, cli/app, letta_adapter, git_history
adapter, routing_replay_bench, report) keep their `BLE001` entry because non-silent broad
handlers remain and clearing them would be the forbidden broad refactor. Phase 23 still
makes their silent sites observable (QBL-EXC-02), but their ignore shrink is explicitly
deferred. Document this in the phase summary so QBL-EXC-03 is judged against the 8
removable files, not all 15.

## Logger Patterns (for minimal local edits)

| File | Existing logger | Executor action |
|------|-----------------|-----------------|
| `gateway/adapters/mcp_server.py` | `logger = logging.getLogger(__name__)` (L50); `_log = logging.getLogger("atelier.mcp")` (L370) | Reuse `_log` for the in-function sites (most are module-level functions) |
| `gateway/cli/app.py` | `logger = logging.getLogger(__name__)` (L65) | Reuse `logger` |
| All other 13 files | **none** | Add `import logging` + `logger = logging.getLogger(__name__)` at module top first |

## Common Pitfalls

### Pitfall 1: Assuming a log call clears BLE001
**What goes wrong:** Executor adds `logger.debug(exc_info=True)`, deletes the per-file
ignore, lint fails because `except Exception` still trips BLE001.
**How to avoid:** Only delete ignore lines for files where every broad handler is
narrowed or `# noqa`-annotated. Use the census table above.

### Pitfall 2: Writing to stdout inside MCP code
**What goes wrong:** A `print()` (or logging configured to stdout) inside `mcp_server.py`
corrupts the JSON-RPC stdio framing the MCP host parses.
**How to avoid:** Use the existing `logging` loggers (stderr by default). Never introduce
`print()` here. `test_mcp_stdio_smoke.py` guards this.

### Pitfall 3: Changing best-effort semantics into hard failures
**What goes wrong:** Narrowing + re-raising in a telemetry/cache-planning path breaks the
caller (e.g. a tool call or host model callback) on a transient failure.
**How to avoid:** For Class **B** sites (savings emit, prefix-cache planning, SDK
callbacks, dashboard), keep suppression and only add an observable log. Reserve
re-raise/surface for genuine error paths, not best-effort side effects.

### Pitfall 4: Over-narrowing parse loops
**What goes wrong:** Narrowing a transcript parser to only `json.JSONDecodeError` lets a
`KeyError`/`TypeError` escape and crash a benchmark run that was previously resilient.
**How to avoid:** Narrow to the realistic tuple
`(json.JSONDecodeError, KeyError, ValueError, TypeError, OSError)` for parse loops.

## Runtime State Inventory

This is a code-only audit/fix pass; no runtime state is renamed or migrated.

| Category | Items Found | Action Required |
|----------|-------------|-----------------|
| Stored data | None — no datastore keys/IDs changed. | None — verified by scope (handler bodies + `pyproject.toml` only) |
| Live service config | None — no service config strings touched. | None |
| OS-registered state | None. | None |
| Secrets/env vars | None — env var *names* unchanged. | None |
| Build artifacts | None — no package/module renames; `pyproject.toml` change is lint-config only, no reinstall needed. | None |

## Test / Validation Plan

**Existing tests for touched modules (verified present):**

| Module(s) | Test file(s) |
|-----------|--------------|
| MCP adapter (required by QBL-EXC-04) | `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/gateway/test_mcp_stdio_smoke.py`, `tests/gateway/test_mcp_jsonrpc_e2e.py`, `tests/gateway/test_mcp_route*.py` |
| SDK middleware | `tests/gateway/test_sdk_middleware.py` |
| Letta adapter | `tests/infra/test_letta_adapter_fallback.py`, `tests/infra/test_memory_adapters.py`, `tests/core/test_letta_adapter_stub.py` |
| Memory backend selection (environment.py) | `tests/infra/test_memory_backend_selection.py` |
| Code context engine | `tests/core/test_code_context.py` |
| git_history adapter | `tests/infra/code_intel/git_history/test_*.py` |
| Benchmarks (savings replay) | `tests/infra/test_savings_replay.py`, `tests/benchmarks/` |

**Baseline (measured this session):** `tests/gateway/test_p0_mcp_surfaces.py` →
**35 passed** in ~3s. `uv run ruff check src` → **exit 0** (clean; per-file-ignores
active). Ruff version **0.15.14**. No pre-existing blockers found on these surfaces.

**Focused validation commands (per touched surface):**
```bash
# Lint must stay green after each ignore-line deletion:
uv run ruff check src --select BLE001
# Required MCP/tool-handler coverage (QBL-EXC-04):
uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_stdio_smoke.py -q
# SDK + memory + environment touched modules:
uv run pytest tests/gateway/test_sdk_middleware.py tests/infra/test_letta_adapter_fallback.py tests/infra/test_memory_backend_selection.py -q
# Re-run the enumeration to confirm pass-count drops to 0 (or each survivor has # noqa):
grep -rn -A1 "except Exception" src --include='*.py' | grep -B1 "pass" | grep -c "except Exception"
```

**Phase gates:** `make lint && make typecheck && make test`.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (with pytest-xdist when available) |
| Config file | `pyproject.toml` ([tool.pytest...]) + `Makefile` targets |
| Quick run command | `uv run pytest tests/gateway/test_p0_mcp_surfaces.py -q` (~3s, 35 tests) |
| Full suite command | `make test` (xdist `-n auto --dist=loadfile` when present) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| QBL-EXC-01 | Fresh enumeration captured | smoke | `grep -rn -A1 "except Exception" src --include='*.py' \| grep -B1 "pass" \| grep -c "except Exception"` | ✅ (shell) |
| QBL-EXC-02 | Silent sites observable / narrowed | unit | `uv run pytest tests/gateway/test_sdk_middleware.py tests/infra/test_letta_adapter_fallback.py -q` | ✅ |
| QBL-EXC-03 | Ignores shrink for fixed files | lint | `uv run ruff check src --select BLE001` (green after deletions) | ✅ |
| QBL-EXC-04 | MCP/tool-handler surfaces covered | integration | `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_stdio_smoke.py -q` | ✅ |

### Sampling Rate
- **Per task commit:** focused command for the touched module + `uv run ruff check src --select BLE001`.
- **Per wave merge:** `uv run pytest tests/gateway -q` + full ruff check.
- **Phase gate:** `make lint && make typecheck && make test` green before `/gsd-verify-work`.

### Wave 0 Gaps
- [ ] Optional regression test: a previously-swallowed error in one Class-N site now
      surfaces/logs (M2 suggests "if cheap"). Candidate: `environment.py:161` invalid
      config → assert `logger.warning` emitted (caplog). Covered area already has
      `test_memory_backend_selection.py` to extend.
- [ ] No new framework install required — pytest + ruff already present.

*(Otherwise: existing test infrastructure covers all phase requirements.)*

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| `except Exception: pass` | Narrow types, or keep + `logger.debug(..., exc_info=True)` + rationale | Failures become observable; Ruff `BLE001` enforces no new debt |
| One global per-file-ignore list | Per-file ignore removed as each file is cleaned (phase-by-phase burn-down) | Reviewable, ratcheting debt reduction |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The 52 `continue` sites are out of primary scope except the 3 inside removable benchmark files | Inventory / Scope | If planner wants all silent suppressions fixed, scope expands ~2.8×; flag during discuss |
| A2 | Class **B** sites should keep suppression (not surface) because they are genuine best-effort side effects | Classification | Surfacing them could break tool calls / host callbacks |
| A3 | Letta "not found" has a specific SDK exception worth narrowing to | Sites 17/18 | If not, fall back to keep+log; executor verifies against installed Letta SDK |
| A4 | Phase 24 owns `T201` print burn-down, so `T201` tokens stay in ignores | BLE001 shrink plan | If T201 is in-scope here, mcp_server/report ignores need different handling |

## Open Questions (RESOLVED)

1. **Scope of `continue` sites** — Is Phase 23 limited to the 28 `pass` sites (M2 literal),
   or all 80 silent suppressions? RESOLVED: 28 `pass` required + the 3 `continue`
   sites needed to clear removable benchmark files; defer the other 49 `continue` sites.
2. **Narrow vs. keep+noqa for the 8 removable files** — RESOLVED: narrow (cleaner,
   no lingering broad handler, no `# noqa` noise). Executor confirms each narrowed tuple
   doesn't let a real failure escape a best-effort path.

## Sources

### Primary (HIGH confidence)
- Live AST analysis of `src/**/*.py` (this session) — site inventory, per-file broad-handler census, logger presence.
- `uv run ruff check src` (ruff 0.15.14) — baseline lint green; per-file-ignores active.
- `uv run pytest tests/gateway/test_p0_mcp_surfaces.py` — 35 passed baseline.
- `pyproject.toml` lines 110–214 — current BLE001/T201 per-file-ignore ledger.
- `Makefile` — `lint`, `typecheck`, `test` targets and `uv run` conventions.
- `docs/plans/quality-and-benchmark-lift/M2-silent-except-audit.md` — source-of-truth scope.
- `.planning/REQUIREMENTS.md` (QBL-EXC-01..04), `.planning/ROADMAP.md` (Phase 23).

## Metadata

**Confidence breakdown:**
- Inventory & census: HIGH — direct AST + grep cross-check (28 confirmed both ways).
- BLE001 shrink mechanics: HIGH — verified ruff applies handler-level BLE001 regardless of body; census drives removability.
- Classification: MEDIUM-HIGH — based on read code context + comments; a few (letta SDK exception type) need executor confirmation.

**Research date:** 2026-05-29
**Valid until:** Source tree changes invalidate line numbers — re-run the AST scan at execution time before editing.
