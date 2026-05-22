# M7 — `memory op="recall_symbol"` (code + memory + traces, fused)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M2. Stub — flesh out on claim.

## Goal

When an agent walks up to `AuthService`, give it not just the current code
but the *why*: past decisions, prior edits, related postmortems, relevant
trace excerpts. This is Atelier's structural moat — Serena/SCIP/ast-grep
have no memory.

No new MCP tool. Pick one of two homes on claim:

- **Option A:** add `op="recall_symbol"` to existing `tool_memory`
  (mcp_server.py:1380). Best if the fused payload is conceptually
  memory-shaped (returns `MemoryBlock[]` + extras).
- **Option B:** add `op="recall"` to existing `tool_code`. Best if agents
  conceptually reach for "code intel" not "memory" for this.

Lean **Option A** — `memory.recall_symbol` reads naturally and keeps memory
tools cohesive. Confirm with the claiming agent.

## Tool shape (Option A)

```python
tool_memory(
    op="recall_symbol",
    name: str,                                  # required
    include: list[Literal["definition","memory","traces","decisions","tests"]] | None = None,
    horizon_days: int = 180,
    budget_tokens: int = 3000,
    repo: str | None = None,
) -> RecallBundle {
    target: SymbolRecord,
    memory_blocks: list[MemoryBlock],         // tagged with symbol_id
    traces: list[TraceExcerpt],
    decisions: list[DecisionExcerpt],         // docs/decisions/*.md matches
    tests: list[SymbolRecord],                // tests exercising the target
}
```

## Implementation sketch

1. Resolve name via `_code_context_engine().search_symbols(name, limit=1)` (M2-hardened).
2. Memory: `memory_store.search_passages(agent_id, tags=["symbol:<symbol_id>"])` — see "Tag schema" below.
3. Traces: query traces store for ones touching `hit.file_path` within horizon. The ledger already records `file_event` entries.
4. Decisions: ripgrep over `docs/decisions/*.md` for `hit.name` (cheap).
5. Tests: engine query for symbols whose `file_path` starts with `tests/` and whose body references `hit.name`.
6. Budget-pack via M0's packer.

## Tag schema

Use the existing `MemoryBlock.metadata` free-form dict. Convention:

```python
metadata["symbol_ids"] = [hit.symbol_id, ...]      # list — one block can touch many symbols
metadata["symbol_repo"] = "atelier"                # for M10 multi-repo
metadata["origin"] = "edit" | "pattern" | "postmortem" | "bootstrap"
```

Hooks that auto-tag (added by other milestones):
- **M4 (symbol edit)** — every successful symbol edit writes a memory block with the edited `symbol_id`.
- **M5 (pattern rewrite)** — every rewrite writes a block with all touched symbol ids.
- **M11 (bootstrap)** — pinned blocks for top-N symbols.

Tag-by-tag retrieval uses an existing `MemoryStore` extension; if a `tags`
parameter doesn't already index `metadata.symbol_ids`, M7 includes a small
schema migration.

## To flesh out on claim

- Confirm `MemoryStore.search_passages` supports `tags` filter for arbitrary metadata keys, or add a small extension.
- Decision matching: substring vs symbol-aware (avoid `Auth` matching `BasicAuth`). Lean: word-boundary regex.
- Token allocation per `include` category (memory gets most, traces least). Define in `budget.py` policy.
- Default `include` value when caller omits.

## Validation

Tests under `tests/core/capabilities/code_context/test_recall_symbol.py` (or equivalent under memory tests):

- `test_recall_returns_definition_and_tagged_blocks` — fixture: seed a memory block with `symbol_ids=[id]` → recall returns it.
- `test_recall_budget_packs` — large memory set + small budget → truncated, definition preserved.
- `test_decision_match_word_boundary` — `Auth` does not match `BasicAuth` decision doc.
- `test_tests_match` — symbol present in a `tests/` file → included under `tests`.

## Exit criteria

- Either `memory op="recall_symbol"` or `code op="recall"` registered.
- Fixture-driven test passes; tagged memory blocks surface in the bundle.
- Validation matrix row added.

## Open questions

- **Where to register:** Option A (`memory`) vs Option B (`code`). Decide on claim.
- **Cross-repo recall:** when M10 lands, do we recall across all repos in the workspace by default or only the symbol's own repo? Lean: own repo only; explicit `repo="*"` for cross-repo.
