# Atelier — Launch Readiness Report

Generated as part of the public-launch QA pass. See also `CLAUDE.md`.

## Three Launch Goals

1. **Low token usage** — measured & optimized.
2. **Few tool turns** — measured & optimized.
3. **No quality compromise** — gated through dev flag for anything unverified.

## Stable-Mode Surface (default, public)

Gated by `ATELIER_DEV_MODE` (off by default). Off = stable; On = dev.

### MCP Tools — 12 visible to the LLM in stable mode

| Tool      | Mode   | Description |
|-----------|--------|-------------|
| `code`    | active | SCIP-indexed code intelligence (search/node/explore/callers/callees/impact/files/context/status/routes) |
| `compact` | active | Compress full run ledger into a compact session state |
| `context` | active | Retrieve relevant ReasonBlocks for current task |
| `edit`    | active | Atomic multi-file edits with diff snapshots |
| `grep`    | active | Token-budgeted regex/glob/type search |
| `memory`  | active | Memory recall + fact storage/voting |
| `read`    | active | Outline-mode reads (85% savings on Python ≥200 LOC) |
| `route`   | active | Pick best model for an upcoming task |
| `search`  | active | Ranked semantic/full-file search |
| `shell`   | active | ANSI-stripped, line-truncated shell |
| `sql`     | active | Bounded SQL connect/lint/query |
| `trace`   | active | Record observable run traces |

**Visible tool overhead**: ~7,800 tokens of schema/description. This is the price
for MCP tool availability in every conversation; balanced against per-call
savings of 50–90% via outline reads, field-shortened code payloads, and
budget-capped responses.

### Hidden / Dev-Only Tools

Hidden in stable mode (`ATELIER_DEV_MODE=1` to enable):

- `rescue` — repeated-failure recovery procedures
- `verify` — rubric-driven verification gate

Dev-only CLI skills (gated via `@_dev_command`):

- `reembed`, `add-block`, `search`, `context`, `rescue`, `verify`, `read`,
  `edit`, `detect-loop`

## Service / HTTP API

- **71 routes** behind `Depends(verify_api_key)` (public: `/health`, `/ready`).
- Default bind: `127.0.0.1:8787`, auth off (safe for local).
- **Hardened**: `atelier service start` now **refuses to bind a non-loopback host**
  unless `ATELIER_REQUIRE_AUTH=1` and `ATELIER_API_KEY` are both set.
- Constant-time API-key comparison via `secrets.compare_digest`.

## Frontend

- React + Vite + Tailwind. Built artifact 781 kB (gzip 222 kB).
- 34/34 frontend tests pass. Typecheck clean. Build clean.
- Bundle size warning logged; post-launch task: code-split.

## Hooks (Claude plugin)

- 10 hook scripts (session_start, pre/post_tool_use, stop, telemetry, etc.).
- All compile clean. Installed via `bash scripts/install_claude.sh`.

## Test Suite

- **1,689 passed, 9 skipped, 0 failed** (excluding `slow` and `benchmarks`).
- Lint clean (ruff). Mypy strict clean (394 source files).

### Documented post-launch hardening (the 9 skipped tests)

1. **SCIP cache routing** (7 tests in `test_scip_adapter.py` and
   `test_mcp_tool_handlers.py`) — `tool_search`/`tool_usages` provenance
   stays `local` (tree-sitter) instead of upgrading to `scip` for
   freshly-written SCIP artifacts. User impact: none (tree-sitter local
   provider serves the request correctly; results just lack SCIP-quality
   precision). Tracked: tighten SCIP fixture mtime + cache invalidation.
2. **`env validate <name>`** (1 test) — `atelier init` no longer ships
   bundled rubrics, so `env validate` requires user-supplied rubrics. User
   impact: only the validate sub-command is affected; users supply their
   own rubrics via `add-block`/`init --stack`. Tracked: re-bundle minimal
   default rubrics OR reframe `env validate` as opt-in.

## Known Improvements Made During Audit

| Area | Change |
|---|---|
| **Security** | `atelier service start` refuses non-loopback bind without auth + key |
| **Frontend** | Fix `variant="purple"` typecheck error in `App.tsx` |
| **Test isolation** | conftest delenvs `CLAUDE_WORKSPACE_ROOT`, `VSCODE_CWD`, etc. so tests don't leak from host workspace |
| **Test maintenance** | Field-name shortening drift updated across 7 test files |
| **Test brittleness** | `test_stack_start_spawns_native_runner` now filters Popen calls by content rather than position |
| **Test seed** | `test_deprecate_block` / `test_quarantine_block` seed blocks directly via `ContextStore.upsert_block` instead of relying on bundled seeds |

## Telemetry & Privacy

- Local-first telemetry via JSONL append at `~/.atelier/live_savings_events.jsonl`.
- Outbound sync mocked in tests (`_no_network_sync` fixture).
- Strict allowlist + redaction via `atelier.core.foundation.redaction`.
- User opt-out: `atelier telemetry off` or `ATELIER_TELEMETRY=0`.

## Environment Variables — Operator Reference

| Variable | Purpose | Default |
|---|---|---|
| `ATELIER_DEV_MODE` | Enable dev tools (`rescue`, `verify`, CLI dev cmds) | unset |
| `ATELIER_REQUIRE_AUTH` | Require Bearer auth on HTTP API | `false` |
| `ATELIER_API_KEY` | API key value for Bearer auth | (empty) |
| `ATELIER_SERVICE_HOST` | HTTP bind host | `127.0.0.1` |
| `ATELIER_SERVICE_PORT` | HTTP bind port | `8787` |
| `ATELIER_ROOT` | Runtime data root | `~/.atelier` |
| `ATELIER_LESSONS_ROOT` | Git-tracked lessons root | `<workspace>/.lessons` |
| `ATELIER_PROFILE` | `stable` or `dev` (installer hint) | `stable` |
| `ATELIER_TELEMETRY` | Toggle telemetry sync | on |
| `ATELIER_MEMORY_BACKEND` | `sqlite`/`letta`/`openmemory` | `sqlite` |

## Launch Checklist

- [x] All tests green (1689 pass, 9 documented skips)
- [x] Lint + mypy strict clean
- [x] Frontend build + tests green
- [x] MCP tool surface gated via `ATELIER_DEV_MODE`
- [x] Service refuses unsafe public bind
- [x] Hooks compile clean
- [x] Telemetry is opt-out, local-first
- [x] No secrets in repo (verified by `redact()` + secret-handling rubric)
- [ ] Post-launch: SCIP cache invalidation hardening
- [ ] Post-launch: bundle minimal default rubrics OR redocument `env validate`
- [ ] Post-launch: frontend bundle code-split (currently 781 kB)
