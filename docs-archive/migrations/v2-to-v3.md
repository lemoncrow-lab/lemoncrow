# V2 to V3 Migration

> **Architectural anchor:** Atelier does not run an agent loop, does not call
> LLMs, does not spawn subagents, and does not hold API keys. V3 preserves this
> boundary unchanged. Any packet or contribution that violates it is out of scope.

## Summary

V3 hardens memory, benchmark, and context-compression behavior without adding
new host integrations. The main operator actions are: selecting a single memory
backend, re-embedding legacy stub vectors, and moving savings claims to the
measured replay benchmark.

---

## What stays the same

- MCP behavior is preserved through the consolidated V3 dispatch surfaces:
  `task`, `route`, `trace`, `verify`, `memory`, `search`, `read`, `edit`,
  `sql`, `compact`, `code`, and related host tools.
- The default memory backend is SQLite — users without a Letta sidecar see no
  change in behaviour.
- Trace schema is a strict superset: no fields removed, no required fields added.
- ReasonBlock store, rubric gates, plan-check, and rescue surfaces are unchanged.
- The host CLI owns the agent loop, model invocation, billing, and API keys —
  exactly as in V2.

---

## What changes

| Concern              | V2                                                                                  | V3                                                                                                                                                                                                                                          |
| -------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `stub_embedding`     | SHA-256 hash used as a fake embedding vector in lesson promoter and archival recall | Deleted. All embedding paths go through the `Embedder` protocol (`LocalEmbedder` by default, `NullEmbedder` in CI). New rows carry `embedding_provenance`; legacy rows are flagged `legacy_stub` and ranked by BM25 only until re-embedded. |
| Memory backend       | Implicit dual-write to Letta + SQLite when `ATELIER_LETTA_URL` is set               | Single-primary, chosen by `[memory].backend = "sqlite"` (default) or `"letta"`. No silent fallback — if Letta is configured but unavailable, `MemorySidecarUnavailable` is raised explicitly.                                               |
| Sleeptime summarizer | Template `groupby` + string truncation; counted as a savings lever                  | Replaced with a real Ollama-backed summarizer (sub-path A1) or Letta-delegated summary (A2). If neither is available, `SleeptimeUnavailable` is raised — no silent fallback to the template. Telemetry records net savings only.            |
| Savings claims       | README stated "81% reduction" derived from hand-written YAML constants              | Retracted. README now cites the measured baseline from `make bench-savings-honest`. `benchmarks/swe/prompts_11.yaml` may not contain `reduction_pct` constants.                                                                             |
| Lesson promoter      | Clustered by SHA-hash fingerprint; precision target never met                       | Rebuilt on real embeddings; precision ≥ 0.7 on the 200-trace fixture.                                                                                                                                                                       |
| Letta as primary     | Dual-write proxy; passage writes bypassed Letta                                     | Full single-primary path when `backend=letta`. Self-hosted Docker setup via `atelier letta up`.                                                                                                                                             |

---

## Step-by-step migration

### 1. Back up your database

```bash
cp .atelier/atelier.db .atelier/atelier.db.v2-backup
```

### 2. Upgrade Atelier

```bash
pip install --upgrade "atelier[vector,memory,smart]"
# or with uv:
uv add "atelier[vector,memory,smart]"
```

Run the normal store initialization (happens automatically on first use, or
force it):

```bash
atelier --root .atelier version
```

This applies any schema migrations (adds `embedding_provenance` columns, creates
`benchmark_run` and `benchmark_prompt_result` tables).

### 3. Re-embed legacy stub vectors

Run a dry scan to see how many legacy rows exist:

```bash
atelier --root .atelier reembed --dry-run --json
```

When the scan reports `legacy_stub` rows, re-embed them:

```bash
atelier --root .atelier reembed --json
```

This is a one-time operation. New rows written after upgrade always carry a real
embedding. Rows that cannot be re-embedded (missing source text) are left with
`embedding_provenance = "legacy_stub"` and continue to be ranked by BM25 only.

### 4. Choose a memory backend (optional)

**SQLite (default — no action needed):**

```toml
# atelier.toml — this is the default; you may omit it entirely
[memory]
backend = "sqlite"
```

**Letta primary — requires a running Letta sidecar:**

```bash
# Start the bundled self-hosted Letta sidecar:
atelier letta up

# Configure Atelier to use it:
export ATELIER_MEMORY_BACKEND=letta
export ATELIER_LETTA_URL=http://localhost:8283
```

Or set in `atelier.toml`:

```toml
[memory]
backend = "letta"
```

If Letta is selected but unavailable, operations raise `MemorySidecarUnavailable`
rather than silently falling back to SQLite. This is intentional: silent
fallbacks were the V2 bug.

### 5. Run periodic consolidation

```bash
atelier --root .atelier consolidate --since 7d --json
```

### 6. Verify measured savings

```bash
make bench-savings-honest
```

This runs the replay benchmark and persists a `BenchmarkRun` row. The README
links to this measured number, not a hand-written constant.

---

## Rollback

If you need to revert to V2:

1. **Restore the database backup:**

   ```bash
   cp .atelier/atelier.db.v2-backup .atelier/atelier.db
   ```

2. **Downgrade the package:**

   ```bash
   pip install "atelier==<last-v2-version>"
   ```

3. **Unset V3 environment variables** (`ATELIER_MEMORY_BACKEND`, etc.) if you
   set them during migration.

The V2 schema is a strict subset of V3. The added columns (`embedding_provenance`,
`benchmark_run`, `benchmark_prompt_result`) are ignored by V2 code. Re-embedding
is not reversible — if you roll back, lesson clustering and archival recall
revert to BM25-only for all rows (same behaviour as un-re-embedded V3 rows).

---

## Notes

- `stub_embedding` is removed from runtime code and rejected by storage for new
  rows. Any plugin or extension that called it must migrate to the `Embedder`
  protocol.
- Letta is not a fallback cache; it is a primary backend when selected.
- Historical V2 savings docs (`docs/benchmarks/v2-context-savings.md`,
  `docs/benchmarks/phase7-2026-04-29.md`) remain as correction-marked archival
  references. Do not delete them — they are referenced from V2 traces.
