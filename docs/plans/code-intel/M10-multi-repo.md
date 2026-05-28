# M10 — Multi-repo workspaces

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Adds an optional `repo` param to existing `tool_code` ops; introduces
> `.atelier/workspace.toml`. **No new MCP tool.** Stub — flesh out on claim.

## Goal

Let one Atelier session navigate symbols across multiple sibling repos
(monorepo subprojects, worktrees, micro-service constellation). SCIP indexes
are per-repo files; the store just needs to know about more than one.

## Approach

- `.atelier/workspace.toml`:
  ```toml
  [workspace]
  id = "leanchain-main"

  [[workspace.repos]]
  name = "atelier"
  path = "."

  [[workspace.repos]]
  name = "billing"
  path = "../billing"
  ```
- `SymbolIntelStore` keeps one reader per repo, queries union them when `repo` filter is absent.
- All tool signatures already include `repo: str | None`.
- SCIP indexers run per repo, output to `.atelier/cache/scip/<repo_name>/`.

## To flesh out on claim

- Cross-repo symbol IDs (prefix with `repo_name::`).
- Disambiguation when same name exists in multiple repos.
- Permissions: read-only repos vs editable ones (flag in config).

## Exit criteria

- Two-repo fixture: `symbol("SharedConfig")` returns hits from both, tagged with repo.
- `repo="atelier"` filter narrows correctly.
- Validation matrix row added.
