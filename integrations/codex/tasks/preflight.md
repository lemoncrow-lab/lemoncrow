# Atelier Codex Preflight

Use this task file to force an Atelier preflight before implementation:

1. Run `context` with task, domain, files, and likely tools.
2. Run `memory` to check archival memory for relevant past findings before reading files.
3. Draft a concrete plan.
4. If domain is high risk (`beseam.shopify.publish`, `beseam.pdp.schema`, `beseam.catalog.fix`, `beseam.tracker.classification`), run `verify` before finalizing.
5. Record the run with `record`.
6. Archive key findings with `memory` for future runs.

Default tool posture: start coding tasks with `context`, use `explore` for code intelligence (`explore` folds in single definitions, callers, callees, and usages), `search` / `grep` for discovery, `read` for file reads, and `edit` for changes. Keep native `Read`, shell `rg`, `grep`, and direct file access as fallback only when the Atelier equivalent is hidden, unavailable, or returned `noop`. The kill switch is `ATELIER_CACHE_DISABLED=1`.
