# Headroom cache-safe context compression

Status: phase 1 implemented behind a feature flag.

## Architecture

Tool execution -> LemonCrow semantic renderers/RTK -> optional fresh-result Headroom compression -> Claude context -> provider cache -> model.

LemonCrow remains authoritative for correctness-sensitive code/search/graph/rescue/bash output. Headroom is a replaceable second-stage optimizer for residual generic tool results only.

## Phase 1: safe experiment

- `LEMONCROW_CONTEXT_COMPRESSOR=none|headroom`, default `none`.
- No LemonCrow package dependency on Headroom. When this backend is selected, `headroom-ai` is discovered dynamically; install it alongside LemonCrow only for the experiment.
- Canonical session history remains raw.
- Wire history keeps the previously-compressed prefix byte-stable for provider caching.
- `code_search`, read, grep, graph/context/rescue, bash, edits, and related navigation output are restored byte-for-byte after Headroom.
- Generic tool output changed by Headroom is first persisted to LemonCrow's spill store. The model receives the compact result plus the normal LemonCrow `full: <path>` recovery pointer.
- A failed spill or failed Headroom transform falls open to the original tool result.
- Optimization traces record incremental estimated tokens before/after, changed/protected message counts, and Headroom transforms.

## Benchmark gate before wider rollout

Compare four arms on the same tasks and machines:

1. raw host/tool output
2. RTK-only shell compaction
3. current LemonCrow semantic rendering
4. LemonCrow semantic rendering + Headroom

Primary measurements: task success, turns, provider input/output tokens, cache-read/write tokens, wall latency, tool latency, and incremental Headroom savings after LemonCrow. Do not count Headroom's savings against raw output as incremental LemonCrow savings.

The rollout gate is task-quality parity plus positive end-to-end cost/latency value. If Headroom only saves tokens already removed by LemonCrow, leave it optional/off.

## Phase 2: external-host benchmark boundary

Implemented for the Claude Code Harbor adapter so the four benchmark arms are materially different rather than labels over the same host behavior:

- `raw`: vanilla Claude Code; no LemonCrow plugin/MCP and no RTK hook.
- `rtk`: vanilla Claude Code plus RTK's Claude `PreToolUse` Bash rewrite hook.
- `lemoncrow`: LemonCrow plugin/MCP plus LemonCrow's RTK-aware Bash path.
- `lemoncrow-headroom`: the exact LemonCrow arm plus Headroom only at the newly-produced MCP tool-result boundary. Claude still talks directly to Anthropic; no provider proxy is used.

Headroom still lives in a separate `/opt/headroom-venv`, currently pinned by `LEMONCROW_BENCH_HEADROOM_VERSION` (default `0.37.0`) so its dependency graph cannot perturb LemonCrow. The Claude plugin `PostToolUse` hook sees only the newly-produced LemonCrow MCP result. It never sees or rewrites the Anthropic request, system prompt, tool schemas, prior history, or already-forwarded messages. This preserves Claude/Anthropic prompt-cache identity by construction rather than trying to repair it after whole-request compression.

The current model-facing LemonCrow surface is intentionally tiny: `bash`, `code_search`, `edit`, `read`, and `web_fetch`. LemonCrow remains authoritative for `code_search`, `read`, `edit`, and `web_fetch`; Headroom never rewrites those results. `web_fetch` already has its own query relevance, summary tier, dynamic budget, and spill recovery.

Headroom is therefore a **residual Bash / foreign-MCP optimizer**, not a second LemonCrow semantic compressor. Residual Bash evaluation starts only after LemonCrow/RTK rendering, only above 8 KiB, and skips any result that already carries LemonCrow compaction/spill markers. The default mode is shadow telemetry (`LEMONCROW_HEADROOM_MCP_TAIL_MODE=shadow`); actual replacement requires explicit `apply`. A candidate must save at least 1,000 estimated tokens and at least 35% before it is considered worthwhile. Initially only Headroom-recognized build/log output is eligible on LemonCrow Bash.

Large foreign MCP results remain the stronger Headroom use case because LemonCrow did not semantically author those payloads. When Headroom is installed, supported build/log, structured JSON, or search-result payloads get a Headroom candidate first; if it is not materially better, unavailable, or unsupported, the existing deterministic LemonCrow head/tail spill remains the fallback. Any accepted lossy result is reversible through the normal `full:` spill pointer.

This mirrors Headroom's own MCP integration principle: compress a tool result before it is added to model context. Headroom's conversation-level `frozen_message_count`/session replay machinery is not needed for this Claude Code path because LemonCrow does not rewrite previously-forwarded messages at all.

Harbor records `context_arm` in `config.agents[].kwargs`. `bench_mode=off` remains a compatibility alias for old jobs; the CLI's legacy `--baseline` maps to `raw` for Claude Code.

Run the arms explicitly:

```bash
lc benchmark harbor --context-arm raw -y
lc benchmark harbor --context-arm rtk -y
lc benchmark harbor --context-arm lemoncrow -y
lc benchmark harbor --context-arm lemoncrow-headroom -y
```

Compare only common tasks across all four jobs:

```bash
uv run python benchmarks/harbor/compare_context_arms.py
```

The comparison separates provider-billed cost/tokens from Headroom's own compression counter. `headroom tokens_saved` is diagnostic incremental compression, not an end-to-end savings claim.

### Harbor portability fixes found during phase 2

The real bundle/preflight validation exposed two independent benchmark-infrastructure drifts and they are fixed as part of the phase:

- Bullseye APT is pinned to the final Debian 11 LTS snapshots (`20260831T235959Z`) so the old-glibc portable build no longer depends on rotating live Bullseye mirrors.
- The portable source copy now satisfies LemonCrow's uv workspace layout (`client`, integrations, vendor, benchmark workspace metadata).
- Non-git Harbor task directories are explicitly registered before code-index prewarm, matching current workspace isolation instead of bypassing it.

Validation completed without model credits: the fresh Bullseye bundle builds, packages both venvs, and the zero-credit setup preflight passes Claude CLI parsing, Headroom health, RTK detection, git/non-git/empty indexing, and log staging.

## Later phases

Conversation/live-zone prose compression is intentionally out of phase 1. Add it only after the tool-result experiment passes quality/cache tests and after it has the same LemonCrow-owned reversible retrieval guarantee.

Evaluate Headroom SharedContext separately for author/reviewer/subagent handoffs. Prefer LemonCrow content/spill references as the authoritative originals rather than maintaining a second retrieval store.
