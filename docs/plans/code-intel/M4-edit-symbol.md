# M4 — `edit` rich descriptor `kind="symbol"` (span-based edits)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M0, M1. Stub — flesh out on claim.

## Goal

Symbol-level edits using positions from `CodeContextEngine.get_symbol`. No
new MCP tool — `tool_smart_edit` (mcp_server.py:1533) already routes
"rich" descriptors through `apply_rich_edits` in
`core/capabilities/tool_supervision/rich_edit.py`. We add a new descriptor
kind there.

## What changes

### `tool_smart_edit` — already accepts arbitrary rich descriptors

No signature change. The dispatcher in `apply_rich_edits` is extended.

### `rich_edit.py` — add `kind="symbol"` handler

Descriptor shape:
```json
{
  "kind": "symbol",
  "name": "AuthService.verify",           // or symbol_id / qualified_name
  "mode": "replace",                       // replace | prepend | append
  "new_body": "def verify(...): ...",
  "preserve_signature": false,
  "repo": null
}
```

Handler logic:
1. Resolve via `_code_context_engine().get_symbol(symbol_name=..., qualified_name=...)`.
2. If multiple matches → fail with structured `disambiguation_required` error (do not silently pick).
3. Read file bytes `[hit.start_byte, hit.end_byte]`.
4. Compute new content per `mode`. Preserve leading indentation of the original block.
5. Hand off to the same atomic write path the existing rich-edit handlers use (so snapshot + diff recording from `tool_smart_edit` apply unchanged).
6. Trigger reindex via M1's SCIP watcher (or engine reindex if SCIP not active).
7. Tag a memory block `edits/<symbol_id>` with the current trace ID (`_get_ledger()`).

The existing `_compute_and_record_diffs` (mcp_server.py:1500) and rollback
behavior apply automatically — that's the whole point of going through
`rich_edit.py` instead of writing a new tool.

## To flesh out on claim

- Conflict semantics: file mtime changed between `get_symbol` and write — retry once or fail with `concurrent_edit` error? Lean: fail with diff.
- `preserve_signature` byte-range math (signature ends at first `:` for Python, first `{` for C-family, end of `=>` arrow for TS arrow funcs). Use tree-sitter if available, regex fallback.
- Indentation detection: tree-sitter parent-node based, not regex.
- Multi-hit safeguard: refuse to edit when `get_symbol` returns ambiguous match unless caller passed `symbol_id`.

## Validation

Tests under `tests/core/capabilities/tool_supervision/test_rich_edit_symbol.py`:

- `test_symbol_replace_diff_matches` — replace method body → diff in ledger matches expected.
- `test_symbol_disambiguation_error` — two symbols named `verify` → returns `disambiguation_required`, no write.
- `test_symbol_renamed_out_of_band` — rename containing class between resolve and write → fail with `symbol_not_found`, no silent wrong-file write.
- `test_reindex_after_edit` — engine query for the edited symbol reflects new body in the same session.
- `test_memory_block_tagged_on_success` — successful edit → memory block with `symbol_id` metadata exists.

## Exit criteria

- `kind="symbol"` descriptor accepted by `tool_smart_edit`.
- All atomic + rollback + diff behavior of `tool_smart_edit` works unchanged.
- Validation matrix row added for the new descriptor kind.
