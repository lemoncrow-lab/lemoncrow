# Deferred Items

## 2026-05-18

- **Out of scope full-suite failures:** `make test` still reports broad pre-existing failures in unrelated infra suites (`tests/infra/test_openmemory.py`, `test_outcome_capture.py`, `test_project_knowledge_store.py`, `test_realtime_context.py`, `test_run_ledger.py`, `test_runtime_benchmarking.py`, `test_savings_replay.py`, `test_search_read_token_savings.py`, `test_session_report.py`, `test_sleeptime_writes_archival.py`, `test_smart_read_outline_first.py`, `test_store.py`, `test_v2_migrations_sqlite.py`). Phase 01-02 targeted validation, plan exit gate, lint, and typecheck are green; the unrelated full-suite failures were not modified by this plan and were deferred per executor scope rules.
