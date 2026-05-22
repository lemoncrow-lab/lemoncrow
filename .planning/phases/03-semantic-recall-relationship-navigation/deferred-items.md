# Deferred Items — Phase 03 Semantic Recall & Relationship Navigation

## 2026-05-19

- `make typecheck` still reports a pre-existing strict mypy failure in `src/atelier/core/capabilities/archival_recall/symbol_recall.py:309` (`Returning Any from function declared to return "dict[str, Any]"`). This file was not modified by Plan `03-03`, so the failure was left out of scope after focused mypy passed on the files changed for M8.
