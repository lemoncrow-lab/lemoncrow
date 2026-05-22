# M3 — `code op="usages"` (name-resolved references)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M0, M1. Stub — flesh out on claim.

## Goal

One-call references for a named symbol. Resolve via the M2-hardened
`code op="search"`, then query SCIP's reference graph (M1) in microseconds.
Fall back to LSP, then tree-sitter, when SCIP doesn't cover the language.

No new MCP tool. We add `op="usages"` to existing `tool_code`
(mcp_server.py:1914).

## What changes

### `tool_code` — extend signature for the new op

```python
op: Literal[..., "usages"],
# new for usages op:
group_by: Literal["file","caller","none"] = "file",
snippet_lines: int = 3,
# reuse existing: query (= name), kind, limit, budget_tokens, repo_root, file_glob
```

### `CodeContextEngine` — new method `find_references`

```python
def find_references(self, query: str, *, kind=None, repo=None,
                    group_by="file", snippet_lines=3, limit=200,
                    budget_tokens=2000) -> dict[str, Any]:
    # 1. Resolve via self.search_symbols(query, limit=10, kind=kind)
    # 2. If multiple → return structured disambiguation_required payload
    # 3. Route: scip_reader.references(symbol_id) → LSP → tree-sitter fallback
    # 4. Attach snippets, group, budget-pack, cache
```

### Response shape

```json
{
  "target": SymbolRecord,
  "references": [...] | { "file_a": [...], "file_b": [...] },   // shape by group_by
  "truncated": false,
  "cache_hit": false,
  "provenance_breakdown": { "scip": 14, "lsp": 0, "treesitter": 0 }
}
```

## To flesh out on claim

- Disambiguation payload schema (mirror M2's choice).
- Caller-snippet rendering when `group_by="caller"`: need symbol-of-callsite resolution.
- Fallback ordering when SCIP partial-covers the language (Python with C extensions).
- Test fixtures: cross-file references in Python and TypeScript.
- Token-cost benchmark vs `grep -rn + Read`.

## Exit criteria

- `code op="usages"` registered (just a new branch in `tool_code`).
- Benchmark shows ≥ 70% token reduction vs grep-and-read.
- Validation matrix row added.
