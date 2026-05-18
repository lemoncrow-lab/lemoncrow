# M9 — External dependency indexing + `scope="external"`

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Adds a new param to existing ops (`scope` on `code op="search"` and friends).
> **No new MCP tool.** Stub — flesh out on claim.

## Goal

Let agents navigate into `site-packages` / `node_modules` / Cargo deps with
the same tools they use on first-party code. SCIP indexers already support
this; we just need to enable it and route correctly.

## Approach

- `scip-python`, `scip-typescript`, `scip-go` all accept flags to include
  external sources. Add an `[code_intel.external]` config block:
  ```toml
  [code_intel.external]
  python = ["site-packages", "active-venv"]
  javascript = ["node_modules"]
  go = ["GOPATH/pkg/mod"]
  ```
- External indexes land at `.atelier/cache/scip/<repo_id>/external-<lang>.scip`.
- SymbolHits gain `origin: "internal" | "external"` field.
- `scope="external"` includes them; default `"repo"` does not. There is no
  `"all"` scope — callers wanting a union issue separate calls (the
  retrieval cache makes the second one free).
- `edit(op="symbol")` rejects edits on `origin="external"` symbols with a clear error.

## To flesh out on claim

- Per-package lazy indexing (don't index all of site-packages, only packages actually imported).
- Size budget (external index can dwarf first-party; cap at e.g. 500 MB by default).
- Cache invalidation on `pip install` / `npm install` — watch the lockfile.

## Exit criteria

- Tools accept `scope="external"`.
- Fixture: `import requests; requests.get` → `symbol("get", scope="external", file_glob="requests/*")` finds it.
- External edit rejected with helpful error.
- Validation matrix row added.
