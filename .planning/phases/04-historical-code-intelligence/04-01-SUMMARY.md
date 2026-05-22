---
phase: 04-historical-code-intelligence
plan: "01"
subsystem: infra
tags: [pygit2, git-history, sqlite, tree-sitter, testing]
requires:
  - phase: 03-semantic-recall-relationship-navigation
    provides: routed code-intel seams and existing tree-sitter symbol extraction
provides:
  - pinned pygit2 bootstrap seam for Phase 4 history work
  - SQLite-backed graveyard storage for deleted and renamed symbols
  - pygit2 history walking with in-memory blob parsing
  - real git-history fixture coverage for delete, rename, and idempotence
affects: [04-02, 04-03, 04-04, historical-code-intelligence]
tech-stack:
  added: [pygit2]
  patterns:
    - explicit dependency bootstrap via require_pygit2()
    - historical symbol extraction from source text instead of working-tree reads
    - real temporary git repositories for infra history tests
key-files:
  created:
    - src/atelier/infra/code_intel/git_history/__init__.py
    - src/atelier/infra/code_intel/git_history/models.py
    - src/atelier/infra/code_intel/git_history/graveyard.py
    - src/atelier/infra/code_intel/git_history/renames.py
    - src/atelier/infra/code_intel/git_history/walker.py
  modified:
    - pyproject.toml
    - uv.lock
    - src/atelier/infra/tree_sitter/__init__.py
    - src/atelier/infra/tree_sitter/tags.py
    - tests/infra/code_intel/git_history/test_graveyard.py
key-decisions:
  - "Pin pygit2 exactly at 1.19.2 and gate git-history code behind require_pygit2() instead of adding fallback backends."
  - "Parse deleted and renamed blobs through extract_tags_from_text() so history walking never depends on live working-tree files."
patterns-established:
  - "Bootstrap seam: git-history modules call require_pygit2() for explicit dependency ownership."
  - "Graveyard ingestion: rename and delete commits upsert typed SQLite entries keyed by symbol, path, and commit."
  - "History testing: use real git fixtures plus in-memory SQLite to prove semantic behavior cheaply."
requirements-completed: [HIST-01]
duration: 5min
completed: 2026-05-19
---

# Phase 4 Plan 01: Historical Code Intelligence Summary

**Pinned `pygit2` bootstrap plus a real-tested graveyard substrate for deleted and renamed symbol history.**

## Performance

- **Duration:** 5 min
- **Started:** 2026-05-19T11:40:51Z
- **Completed:** 2026-05-19T11:45:58Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Pinned `pygit2==1.19.2`, refreshed `uv.lock`, and made Phase 4 bootstrap explicit with a clear no-fallback error path.
- Added the isolated `git_history/` substrate with typed models, SQLite graveyard storage, rename detection, and a `pygit2` walker.
- Extended tree-sitter tag extraction with a source-text helper and proved delete/rename/idempotence behavior through real git fixture tests.

## Task Commits

Each task was committed atomically:

1. **Task 1: Pin and bootstrap `pygit2` explicitly for Phase 4** - `ae2d074` (test), `0538f31` (feat)
2. **Task 2: Build the isolated graveyard substrate with real git-history fixture tests** - `1e2d6aa` (test), `af926a5` (feat)

## Files Created/Modified
- `pyproject.toml` - pins `pygit2==1.19.2` in the repo dependency set
- `uv.lock` - locks the explicit Phase 4 history dependency
- `src/atelier/infra/code_intel/git_history/__init__.py` - exposes the explicit `pygit2` bootstrap seam
- `src/atelier/infra/code_intel/git_history/models.py` - defines typed graveyard entries
- `src/atelier/infra/code_intel/git_history/graveyard.py` - stores deleted and renamed symbols in SQLite
- `src/atelier/infra/code_intel/git_history/renames.py` - normalizes rename detection through `pygit2`
- `src/atelier/infra/code_intel/git_history/walker.py` - walks commit history and ingests delete/rename symbols from blobs
- `src/atelier/infra/tree_sitter/tags.py` - adds source-text tag extraction for historical blobs
- `src/atelier/infra/tree_sitter/__init__.py` - re-exports the source-text extraction helper
- `tests/infra/code_intel/git_history/test_graveyard.py` - covers bootstrap, delete, rename, source-text parsing, and idempotence

## Decisions Made
- Used exact pinning for `pygit2` so the bootstrap and lockfile are explicit and repeatable for later waves.
- Kept the substrate infra-local under `src/atelier/infra/code_intel/git_history/` and did not touch `engine.py` or `mcp_server.py`.
- Reused the existing tree-sitter extraction seam by adding `extract_tags_from_text()` instead of duplicating parsing logic in the walker.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Context7 CLI was unavailable in this executor environment, so package/API verification used PyPI metadata plus official `pygit2` docs directly before pinning.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Wave 2 can wire deleted-history search onto existing code-intel surfaces assuming `pygit2` is present and the graveyard substrate exists.
- Rename-aware and delete-aware ingestion is proven with real fixtures before any MCP or engine wiring starts.

## Self-Check: PASSED
