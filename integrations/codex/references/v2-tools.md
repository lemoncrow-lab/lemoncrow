# LemonCrow V2 MCP Tools (reference)

V2 adds task-state, environment, monitor, savings, and smart-tool
capabilities.

## Run ledger (per-run task state)

- `context({ session_id })` — returns the current plan,
  hypotheses tried/rejected, verified facts, open questions, blockers,
  next required validation, tool/token counts, file/command/test
  history, and the recent event tail.
- `record({ session_id, op, ... })` — append-only
  setters: `set_plan`, `add_hypothesis` (with optional `rejected`),
  `add_verified_fact`, `add_open_question`, `set_blocker`,
  `set_next_validation`, `record_test`.

Use these instead of restating prior task context in chat.

## Monitors

- `record({ session_id, event })` — pushes a structured
  observation (tool result, command outcome, file edit). Returns any
  monitor alerts (`SecondGuessing`, `Thrashing`, `BudgetExhaustion`,
  `RepeatedFailure`, `WrongDirection`, `OffPlan`, `WrongTool`,
  `ContextRot`).

## Context compression

- `compact({ session_id })` — returns a stable summary of
  the run for re-injection when the live tool log gets long. Replaces
  noisy event scrolls.

## Environments

- `context({ id })` — full Environment definition.
- `context({ domain })` — auto-resolves the
  environment by domain prefix and returns rules, forbidden phrases,
  required validations, attached procedures.

## Smart tools (default-on cache)

- `read({ path, max_bytes? })` — caches reads and tracks
  per-call savings.
- `search({ pattern, path? })` — caches FTS hits.
- `search({ pattern, path?, args? })` — wraps `grep`/`rg`
  with caching and command-injection rejection.
- `code({ op: "index" | "search" | "symbol" | "outline" | "context", ... })` —
  dispatches repository code-index operations through one MCP tool.
- `sql({ connection_alias, sql, params?, row_limit? })` — read-only SQL
  inspection through configured aliases.
- `shell({ command, timeout?, cwd?, max_lines? })` — compact supervised shell
  command execution.

These are default-on LemonCrow augmentations for bounded, repeated context
reads/searches. In Codex, native `Read`, shell `rg`, `grep`, and direct
repository search may still be host-visible, but LemonCrow policy treats them as
fallback-only rather than the preferred path. Set `LEMONCROW_CACHE_DISABLED=1`
to bypass LemonCrow caching.
## Hard rules (additive to workflow.md)

6. Do not omit the task description when resuming a run mid-stream.
7. Do not store hidden chain-of-thought in
   `record` payloads — only observable facts.
8. Do not remove host-native read/search tools; smart tools are an augmentation,
   not a replacement.
