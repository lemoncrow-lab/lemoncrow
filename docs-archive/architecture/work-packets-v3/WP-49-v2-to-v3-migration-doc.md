---
id: WP-49
title: V2→V3 migration doc + deprecation matrix
phase: J
boundary: Migration
owner_agent: atelier:code
depends_on: [WP-33, WP-34, WP-35, WP-36, WP-39, WP-47]
supersedes: []
status: done
---

# WP-49 — V2→V3 migration doc

## Why

V3 is a small cleanup release. The MCP tool surface is unchanged. Most users do nothing on
upgrade. But three things changed under the hood and one CLI command was added:

1. The memory backend is now picked by an explicit config knob (no more dual-write).
2. `stub_embedding` is gone; legacy embedding rows are flagged `legacy_stub` and need a
   one-time `atelier reembed` to be searchable again.
3. The sleeptime "lever" was either replaced with a real summarizer or removed (per WP-36).
4. The 81 % savings headline is retracted (WP-34) and replaced with a measured number (WP-50).

WP-49 collects this into a single migration doc + deprecation matrix that a V2 user can scan
and act on in five minutes. It is also a self-audit: if the doc is incomplete, the test gate
catches it.

## Files touched

- **NEW:** `docs/migrations/v2-to-v3.md` — the migration doc itself.
- **NEW:** `docs/migrations/v2-to-v3-deprecation-matrix.md` — every removed/changed symbol
  with its replacement.
- **EDIT:** `README.md` — top-of-readme link to the migration doc; "If you used V2, read
  this" callout.
- **EDIT:** `CHANGELOG.md` — V3.0 entry referencing the migration doc.
- **NEW:** `tests/docs/test_migration_doc_completeness.py` — programmatic check that every
  symbol marked `deprecated` (Pydantic warning, docstring directive, removed export) in the
  V3 codebase has a matching row in the deprecation matrix.

## How to execute

1. **Walk the codebase for `deprecated` markers.** Pydantic deprecation warnings, docstring
   `.. deprecated::` directives, removed exports, removed CLI flags — collect them all.

2. **Cross-reference V3 plan § 2 (audit findings) and data-model § 6 (deprecation matrix).**
   Every entry there must have a row in the migration doc with:
   - Symbol / file path
   - V2 behavior (one line)
   - V3 behavior (one line)
   - Migration step for an existing user (one line)
   - WP that did it

3. **Write the migration doc.** Structure:
   - **Quick summary** — "Most users do nothing." (Because the MCP surface is preserved.)
   - **What stays the same** — Atelier is still a tool/data provider; the host CLI still owns
     the loop; every V2 MCP tool name, signature, return shape, ReasonBlock schema, trace
     shape are preserved.
   - **What changes** — three items: memory backend config knob, embedding back-fill,
     sleeptime telemetry, savings headline.
   - **Step-by-step:**
     - SQLite users: do nothing on upgrade. Optionally run `atelier reembed` to back-fill
       legacy rows so archival recall recovers cosine ranking on V2 history.
     - Letta users: add `[memory].backend = "letta"` to `.atelier/config.toml` (or set
       `ATELIER_MEMORY_BACKEND=letta`). Confirm `ATELIER_LETTA_URL` is set. Run
       `atelier reembed` if you want V2 archival rows to participate in cosine ranking.
   - **Deprecation matrix** — link to the table doc.
   - **Rollback** — V3 with `[memory].backend = "sqlite"` is V2-equivalent for memory.
     Reverting `stub_embedding` is not supported (it was incorrect); recall continues working
     via BM25 if no embedder is configured.

4. **Cross-link.** README, CHANGELOG, and migration doc all reference each other.

5. **Run the completeness test.** It must catch any deprecation that didn't make it into the
   matrix.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/docs/test_migration_doc_completeness.py -v

# Manual review:
# - Open docs/migrations/v2-to-v3.md, verify it walks a real user through the upgrade in
#   under 5 minutes.
# - Open the deprecation matrix, verify every WP-33..47 deprecation has a row.

make verify
```

## Definition of done

- [ ] Migration doc published; covers stay-same / changes / step-by-step / rollback.
- [ ] Deprecation matrix doc published; one row per deprecated symbol, linked to its WP.
- [ ] README and CHANGELOG cross-link.
- [ ] `test_migration_doc_completeness.py` passes (every codebase deprecation has a matrix
      row).
- [ ] Doc explicitly states "Atelier does not run an agent loop and does not call LLMs;
      the host CLI does." — so future readers don't repeat the V3-draft-v1 confusion.
- [ ] `make verify` green.
- [ ] V3 INDEX updated. Trace recorded.
