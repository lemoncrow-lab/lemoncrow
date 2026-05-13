# Atelier Codex Preflight

Use this task file to force an Atelier preflight before implementation:

1. Run `task` with task, domain, files, and likely tools.
2. Run `memory` to check archival memory for relevant past findings before reading files.
3. Draft a concrete plan.
4. If domain is high risk (`beseam.shopify.publish`, `beseam.pdp.schema`, `beseam.catalog.fix`, `beseam.tracker.classification`), run `verify` before finalizing.
5. Record the run with `trace`.
6. Archive key findings with `memory` for future runs.

Default tool posture: use `search` (Atelier augmentation) for repeated context
reads/searches to save tokens, and `read` for repeatable single-file reads. Leave native `Read`, shell `rg`, `grep`, and direct file
access available for exact raw inspection. The kill switch is `ATELIER_CACHE_DISABLED=1`.
