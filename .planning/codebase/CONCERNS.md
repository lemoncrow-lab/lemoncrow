# Codebase Concerns

**Analysis Date:** 2026-05-23

## Tech Debt

**Monolithic gateway/service modules:**
- Issue: Core operational logic is concentrated in very large files, increasing change blast radius and merge conflict frequency.
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/core/capabilities/code_context/engine.py`
- Impact: Small feature changes require navigating thousands of lines and mixed responsibilities, increasing regression risk.
- Fix approach: Split by bounded contexts (service lifecycle, auth, analytics, ingestion, routing, host integration) and keep each module narrowly scoped.

**Duplicate ingestion implementations:**
- Issue: Session ingestion is implemented in parallel modules with diverging behavior and return payloads.
- Files: `src/atelier/core/service/ingest_session.py`, `src/atelier/core/service/ingest_session_directory.py`, `src/atelier/core/service/worker.py`
- Impact: Different execution paths can produce inconsistent ingest outcomes and metadata.
- Fix approach: Consolidate ingest logic behind one shared service used by both worker queue and directory watcher.

**Typed quality exemptions on high-churn code:**
- Issue: Strict type checking is explicitly disabled for the largest CLI adapter module.
- Files: `pyproject.toml`, `src/atelier/gateway/adapters/cli.py`
- Impact: Refactors in CLI/service orchestration can ship type regressions undetected.
- Fix approach: Remove broad `ignore_errors` override incrementally and gate PRs on typed submodules.

## Known Bugs

**Session ingest reports success without persisting reconstructed events:**
- Symptoms: Ingest calls return success metadata but reconstructed ledger events are not stored as traces.
- Files: `src/atelier/core/service/ingest_session.py`, `src/atelier/core/service/ingest_session_directory.py`
- Trigger: Call `ingest_session_file(...)` through ingest service or worker path.
- Workaround: Re-import via host-specific parsers that persist traces/artifacts.

**Ingestion behavior differs by execution path:**
- Symptoms: Queue-driven ingest and directory-watch ingest use different implementations and payload schemas.
- Files: `src/atelier/core/service/worker.py`, `src/atelier/core/service/ingest_session.py`, `src/atelier/core/service/ingest_session_directory.py`
- Trigger: Run the same session file via queued job vs watched directory.
- Workaround: Standardize on one ingestion entry path operationally until implementation is unified.

## Security Considerations

**Service API unauthenticated mode is default:**
- Risk: API can run with `ATELIER_REQUIRE_AUTH=false` and includes file-read endpoints that accept arbitrary local paths.
- Files: `src/atelier/core/service/config.py`, `src/atelier/core/service/auth.py`, `src/atelier/core/service/api.py`, `tests/gateway/test_service_api.py`
- Current mitigation: Service defaults to `127.0.0.1` bind and supports Bearer auth with constant-time compare when enabled.
- Recommendations: Require auth by default in non-dev profiles, deny-list sensitive paths for `/v1/files/content`, and enforce explicit `ATELIER_REQUIRE_AUTH=true` for background stack installs.

**Automatic self-update executes pulled code in background services:**
- Risk: Runtime performs `git fetch/pull` then executes installer scripts automatically.
- Files: `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/gateway/adapters/cli.py`, `README.md`
- Current mitigation: Opt-out env toggle (`ATELIER_NO_AUTO_UPDATE=1`) and `--ff-only` pull mode.
- Recommendations: Add signed release verification, pin update channels/tags, and require explicit opt-in for auto-update execution.

**Temporary spill/state files under shared temp directories:**
- Risk: Tool payloads and edit-state hints are written to temp paths that may be readable by other local users depending on host permissions.
- Files: `src/atelier/core/capabilities/tool_supervision/native_search.py`, `src/atelier/core/capabilities/plugin_runtime.py`, `src/atelier/infra/code_intel/zoekt/server.py`
- Current mitigation: Uses process temp directory and non-shell subprocess invocation for most execution paths.
- Recommendations: Use per-user private directories under `ATELIER_ROOT` with strict permissions and rotate/delete spill artifacts aggressively.

## Performance Bottlenecks

**Overview endpoints perform repeated JSON-heavy scans:**
- Problem: Analytics/overview handlers combine aggregate SQL with additional per-row payload deserialization loops.
- Files: `src/atelier/core/service/api.py`
- Cause: Multiple passes over trace rows (`SUM(...)` then iterative JSON decode) and broad in-memory aggregation.
- Improvement path: Pre-aggregate cost/token columns, add indexed materialized summaries, and avoid per-request full payload decoding.

**CLI analytics path does full-table reads on traces:**
- Problem: Snapshot generation scans large `traces` tables and deserializes many payloads in-process.
- Files: `src/atelier/gateway/adapters/cli.py`
- Cause: Unbounded table query patterns and follow-up JSON parsing loops.
- Improvement path: Add time-window filters, pagination, and summarized rollups in storage instead of full payload replay.

**Large parser/service modules increase CPU and maintenance overhead:**
- Problem: Large host parser and adapter files carry many broad exception branches and fallback parsing paths.
- Files: `src/atelier/gateway/hosts/session_parsers/_session_parser.py`, `src/atelier/gateway/hosts/session_parsers/copilot.py`, `src/atelier/gateway/adapters/cli.py`
- Cause: Multi-format parsing and orchestration logic coupled into single modules.
- Improvement path: Split per-format handlers, add explicit parse result contracts, and profile hot paths with fixture corpora.

## Fragile Areas

**Broad exception suppression in critical orchestration paths:**
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/gateway/hosts/session_parsers/_session_parser.py`
- Why fragile: Runtime often continues after `except Exception` branches, producing partial outputs without surfacing strong failure signals.
- Safe modification: Replace blanket catches with typed exceptions, structured error events, and explicit fallback status in returned payloads.
- Test coverage: No direct tests detected for auto-update helpers (`_check_auto_update`, `_servicectl_check_and_apply_updates`) in `tests/`.

**Session parser ingest pipeline:**
- Files: `src/atelier/gateway/hosts/session_parsers/_session_parser.py`, `src/atelier/gateway/hosts/session_parsers/gemini.py`, `src/atelier/gateway/hosts/session_parsers/copilot.py`
- Why fragile: High format variance plus silent skip-on-error behavior can lose traces without failing jobs.
- Safe modification: Add parser conformance fixtures per host and fail-fast counters for parse-drop rates.
- Test coverage: Parser edge cases are tested in parts, but ingestion skip/failure telemetry assertions are sparse in `tests/gateway/`.

## Scaling Limits

**Session listing and trace fan-out caps:**
- Current capacity: Session list endpoint enforces `limit <= 1000` and internally fans out trace fetches with capped multipliers.
- Limit: Larger installations can degrade or return partial windows under fixed caps.
- Scaling path: Introduce cursor pagination and incremental summaries for `v1/sessions`.

**Snapshot and import size gates trade completeness for stability:**
- Current capacity: File snapshots skip files above 256 KB; parser import helpers skip massive session files using static byte limits.
- Limit: Large artifact/session coverage drops as project and transcript sizes grow.
- Scaling path: Support chunked snapshot storage and streaming parser ingest with resumable cursors.

## Dependencies at Risk

**`pygit2==1.19.2`:**
- Risk: Exact pin on a native extension dependency increases install/runtime fragility across OS/libgit2 variants.
- Impact: Auto-update, git-aware flows, and repository features can fail on environments where wheels/system libs mismatch.
- Migration plan: Move to compatibility range with CI matrix validation and fallback to `GitPython` path when `pygit2` unavailable.

**Optional parser/code-intel dependencies with fallback behavior:**
- Risk: Features silently downgrade when optional imports are unavailable.
- Impact: Retrieval quality and code-intel outputs can vary by machine setup, reducing reproducibility.
- Migration plan: Define explicit capability health checks at startup and fail closed for required production profiles.

## Missing Critical Features

**Trace persistence in generic ingest services:**
- Problem: Reconstructed session ledger events are not persisted by the ingest service path.
- Blocks: Reliable worker-driven session ingestion for traces/analytics parity.

**Pack command surface in CLI entrypoint:**
- Problem: Pack command registration remains disabled pending refactor.
- Blocks: Unified packaging workflows through top-level CLI path.

## Test Coverage Gaps

**Untested ingest service modules:**
- What's not tested: Direct behavior of generic ingest services and directory watcher lifecycle.
- Files: `src/atelier/core/service/ingest_session.py`, `src/atelier/core/service/ingest_session_directory.py`
- Risk: Regression in session persistence path can ship without detection.
- Priority: High

**Untested auto-update execution paths:**
- What's not tested: Git update checks, pull/apply behavior, and installer invocation guards.
- Files: `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/gateway/adapters/cli.py`
- Risk: Background services may drift into failing update loops or unsafe update behavior unnoticed.
- Priority: High

---

*Concerns audit: 2026-05-23*
