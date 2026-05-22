# M8 — `code op="callers"` / `op="callees"` from SCIP graph

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M1. Stub — flesh out on claim.

## Goal

Walk the call graph from a named symbol without ever asking for a position.
SCIP encodes the full call graph; we just traverse.

No new MCP tool. Two new ops added to `tool_code`.

## What changes

### `tool_code` — extend signature for the new ops

```python
op: Literal[..., "callers", "callees"],
# new params:
depth: int = 1,
snapshot: bool = False,                  # if True, persist a memory block for diffing
# reuse: query (= name), limit, budget_tokens, repo_root
```

### `CodeContextEngine` — new methods

```python
def callers(self, query: str, depth=1, limit=50,
            snapshot=False, budget_tokens=2000) -> dict[str, Any]: ...
def callees(self, query: str, depth=1, limit=50,
            snapshot=False, budget_tokens=2000) -> dict[str, Any]: ...
```

Both:
1. Resolve via `search_symbols`.
2. BFS the SCIP call-edge graph to `depth` (fallback: LSP `prepareCallHierarchy` + `incoming/outgoingCalls`).
3. If `snapshot=True`, hash the edge set, write a memory block tagged
   `call_graph/<symbol_id>` with metadata `{depth, direction, ts}`. Return
   the block id for later diffing.
4. Budget-pack via M0.

### Optional follow-on tool

`code op="call_graph_diff"` reads two snapshots and returns the delta. Useful
for *"who started calling `processPayment` since last week?"*. Skip in M8;
add as a follow-up if there's demand.

## To flesh out on claim

- Cycle detection (recursive functions, mutual recursion).
- Edge metadata (call site line, conditional vs unconditional — SCIP records this for some indexers).
- Snapshot format (compact: sorted list of `(caller_id, callee_id)` tuples; gzip + base64).
- Diff semantics for the optional `call_graph_diff` op.

## Exit criteria

- Both ops accepted by `tool_code`.
- Depth-2 call graph for a fixture symbol returns expected edges.
- Snapshot + (optional) diff round-trip works.
- Validation matrix row added.
