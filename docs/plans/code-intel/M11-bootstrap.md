# M11 — First-context bootstrap

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Runs via existing `core/service/jobs.py` worker, triggered from existing
> `tool_get_context` (mcp_server.py:663). Persists results as pinned blocks
> read via existing `tool_memory`. **No new MCP tool unless `tool_get_context`
> exposing a manual `bootstrap=True` flag is preferred — decide on claim.**
> Stub — flesh out on claim.

## Goal

First time an agent enters a workspace, run all the expensive one-time work
in the background and persist results as pinned memory blocks. Every
subsequent session reads them via normal recall — zero token cost.

## Approach

When `mcp__atelier__context` runs for the first time in a workspace, spawn a
background job that:

1. Runs SCIP indexers for every detected language (M1).
2. Runs `repo_map` PageRank (existing).
3. Embeds top-N symbols (M6).
4. For top-200 ranked symbols, generates a 1-line summary via a small LLM call
   (one batch, not per-symbol round-trips).
5. Detects entry points (`main`, `app`, `cli`, `server`, `index.*`).
6. Writes pinned memory blocks tagged `bootstrap/<repo_id>`:
   - `architecture-sketch` — module tree + import topology
   - `entry-points` — list with file:line
   - `hot-symbols-top-N` — name + signature + 1-line summary
   - `language-mix` — what indexers ran, what didn't

Subsequent sessions get this content via `mcp__atelier__context` automatically.

## To flesh out on claim

- Triggering: explicit `mcp__atelier__bootstrap` MCP tool or implicit on first context?
- Cost cap: bound the bootstrap LLM call cost (per-workspace, recorded).
- Refresh policy: re-run bootstrap after how many commits / how much time?
- Failure mode: if SCIP indexer crashes mid-bootstrap, mark partial completion, retry next session.

## Exit criteria

- Empty cache + cold start: bootstrap job completes, pinned blocks present.
- Second session shows bootstrap blocks in context with zero new tool calls.
- Cost: one-time bootstrap < $0.10 on a 50k-symbol repo.
- Validation matrix row added.
