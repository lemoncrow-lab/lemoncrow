# Codebase Concerns

**Analysis Date:** 2026-05-18

## Tech Debt

**Monolithic command and API surfaces:**
- Issue: Core entrypoints concentrate many unrelated responsibilities in single files: `src/atelier/gateway/adapters/cli.py` (6,953 lines in repo scan), `src/atelier/core/service/api.py` (5,561 lines), `src/atelier/gateway/adapters/mcp_server.py` (2,747 lines), `src/atelier/gateway/hosts/session_parsers/_session_parser.py` (2,133 lines), and `src/atelier/core/capabilities/plugin_runtime.py` (1,816 lines).
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/gateway/hosts/session_parsers/_session_parser.py`, `src/atelier/core/capabilities/plugin_runtime.py`
- Impact: Small changes carry a wide regression surface, duplication accumulates, and future extraction work becomes expensive because behavior is spread across giant modules instead of feature-local packages.
- Fix approach: Split by bounded responsibility first: CLI command groups out of `src/atelier/gateway/adapters/cli.py`, route families out of `src/atelier/core/service/api.py`, host-specific parser helpers out of `src/atelier/gateway/hosts/session_parsers/_session_parser.py`, and auth/update/session-state helpers out of `src/atelier/core/capabilities/plugin_runtime.py`.

**Duplicated authentication logic:**
- Issue: API auth is implemented twice: `src/atelier/core/service/auth.py:18-48` uses `secrets.compare_digest()` and returns `503` when auth is required but unconfigured, while `src/atelier/core/service/api.py:2663-2679` defines an inline `verify_api_key()` that uses plain `!=` and returns `401` in the same situation.
- Files: `src/atelier/core/service/auth.py`, `src/atelier/core/service/api.py`
- Impact: Security behavior and HTTP semantics can drift by entrypoint; fixing auth in one place does not fix the other.
- Fix approach: Remove the inline verifier from `src/atelier/core/service/api.py` and route all HTTP auth through `src/atelier/core/service/auth.py`.

**Storage backend abstraction is incomplete:**
- Issue: `src/atelier/infra/storage/factory.py:24-36` supports `sqlite` and `postgres`, but `src/atelier/core/service/api.py:2698-2708` always instantiates `ContextStore`, and multiple routes query `sqlite3` directly (`src/atelier/core/service/api.py:2742-2763`, `src/atelier/core/service/api.py:3297-3305`, `src/atelier/core/service/api.py:4202-4209`, `src/atelier/core/service/api.py:4641-4648`).
- Files: `src/atelier/infra/storage/factory.py`, `src/atelier/core/service/api.py`
- Impact: Postgres support is partial; API routes and analytics paths are coupled to SQLite internals even when config advertises an alternate backend.
- Fix approach: Create the service store through `src/atelier/infra/storage/factory.py` and move analytics/session lookup queries behind store methods instead of raw `sqlite3` calls.

## Known Bugs

**Postgres-backed service routes are likely to fail or be bypassed:**
- Symptoms: API code assumes `store.db_path` and SQLite JSON functions, which do not exist on the Postgres store API surface.
- Files: `src/atelier/core/service/api.py`, `src/atelier/infra/storage/factory.py`, `src/atelier/infra/storage/postgres_store.py`, `tests/README.md`
- Trigger: Run the HTTP service with `ATELIER_STORAGE_BACKEND=postgres`.
- Workaround: Use the default SQLite backend for HTTP routes until `src/atelier/core/service/api.py` is refactored to honor `create_store()`.

**Session imports can fail quietly during background runs:**
- Symptoms: `servicectl tick` can report zero imported sessions for a host after importer exceptions because `_servicectl_import_sessions()` swallows failures and sets `counts[host] = 0` (`src/atelier/gateway/adapters/cli.py:556-589`). Host parsers also catch and skip malformed sessions in place, for example `src/atelier/gateway/hosts/session_parsers/copilot.py:385-427`.
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/gateway/hosts/session_parsers/copilot.py`, `src/atelier/gateway/hosts/session_parsers/_session_parser.py`
- Trigger: Malformed or unexpected host transcript/session formats during background imports.
- Workaround: Run host imports interactively and inspect stdout/stderr; background `servicectl` currently prioritizes continuation over explicit failure.

## Security Considerations

**Local auth state accepts unverified token payloads and stores them on disk:**
- Risk: `atelier login --token ...` accepts raw JSON, base64-decoded JSON, or arbitrary refresh-token text (`src/atelier/gateway/adapters/cli.py:4515-4538`). `parse_login_token()` only parses structure and never validates token origin or signature (`src/atelier/core/capabilities/plugin_runtime.py:169-190`), then `write_auth_state()` persists `accessToken` and `refreshToken` to `auth.json` (`src/atelier/core/capabilities/plugin_runtime.py:193-196`).
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/capabilities/plugin_runtime.py`
- Current mitigation: `auth.json` and `login_pending.json` are written with mode `0o600` (`src/atelier/core/capabilities/plugin_runtime.py:193-196`, `src/atelier/core/capabilities/plugin_runtime.py:259-279`).
- Recommendations: Treat local token material as sensitive session state, validate signed login payloads before persistence, and keep token parsing out of the CLI surface unless there is server-side verification.

**HTTP API CORS policy is overly permissive for an authenticated service:**
- Risk: `src/atelier/core/service/api.py:2688-2695` enables `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]`, and `allow_credentials=True`.
- Files: `src/atelier/core/service/api.py`, `src/atelier/core/service/config.py`
- Current mitigation: Service host defaults to `127.0.0.1` in `src/atelier/core/service/config.py:35-40`, and most routes depend on API-key auth.
- Recommendations: Restrict allowed origins explicitly, or disable credentialed CORS entirely for local-only deployments.

**Self-update paths execute pulled code automatically:**
- Risk: Both the controller and MCP server mutate the git checkout at runtime. `src/atelier/gateway/adapters/cli.py:625-686` runs `git fetch`, `git pull --ff-only`, and `uv sync`; `src/atelier/gateway/adapters/mcp_server.py:351-456` runs `git fetch`, `git pull --ff-only`, and `scripts/install.sh`.
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/gateway/adapters/mcp_server.py`
- Current mitigation: Auto-update is opt-in for `servicectl run` (`src/atelier/gateway/adapters/cli.py:5825-5849`) and can be disabled for MCP with `ATELIER_NO_AUTO_UPDATE=1` (`src/atelier/gateway/adapters/mcp_server.py:359-366`).
- Recommendations: Keep auto-update disabled in development and production environments with local modifications, and require a staged release channel before enabling checkout mutation on long-running processes.

## Performance Bottlenecks

**SQLite job and analytics paths serialize scale-up:**
- Problem: The main store uses a single SQLite database file with WAL (`src/atelier/core/foundation/store.py:386-390`), and job claiming takes a write lock with `BEGIN IMMEDIATE` (`src/atelier/core/foundation/store.py:1175-1205`).
- Files: `src/atelier/core/foundation/store.py`
- Cause: SQLite remains a single-writer system; queue claims and high-write workloads cannot scale horizontally.
- Improvement path: Keep SQLite for local/dev use, but move queue/analytics-heavy service workloads behind the Postgres abstraction already started in `src/atelier/infra/storage/factory.py`.

**Bulk import mode trades durability and memory for throughput:**
- Problem: `ContextStore.batch_mode()` disables full sync durability and requests a 512 MB SQLite cache (`src/atelier/core/foundation/store.py:301-324`).
- Files: `src/atelier/core/foundation/store.py`
- Cause: Imports optimize for speed with `PRAGMA synchronous = OFF` and a large cache.
- Improvement path: Gate the aggressive PRAGMAs behind explicit batch/import commands only, and emit telemetry when bulk mode is active so operators know durability has been reduced.

## Fragile Areas

**Host session parser stack:**
- Files: `src/atelier/gateway/hosts/session_parsers/_session_parser.py`, `src/atelier/gateway/hosts/session_parsers/copilot.py`, `src/atelier/gateway/hosts/session_parsers/codex.py`, `src/atelier/gateway/hosts/session_parsers/claude.py`, `src/atelier/gateway/hosts/session_parsers/gemini.py`
- Why fragile: Parsers handle many vendor-specific export formats and rely heavily on broad exception handling; representative skip-and-continue behavior is visible in `src/atelier/gateway/hosts/session_parsers/copilot.py:385-427`.
- Safe modification: Add fixture-backed regression tests before changing normalization logic, and prefer host-local helper extraction over more branching in `_session_parser.py`.
- Test coverage: Parser coverage exists, but background import failure handling is still continuation-oriented rather than fail-fast.

**OpenMemory bridge local state:**
- Files: `src/atelier/gateway/integrations/openmemory.py`, `tests/infra/test_openmemory.py`, `tests/README.md`
- Why fragile: `_load_store()` silently resets to an empty structure on any read/JSON error (`src/atelier/gateway/integrations/openmemory.py:70-84`), and `_save_store()` rewrites `openmemory_bridge.json` directly without atomic replace or locking (`src/atelier/gateway/integrations/openmemory.py:87-92`). The repo also documents failing end-to-end OpenMemory tests in `tests/README.md:48-50` and `tests/README.md:134-136`.
- Safe modification: Preserve atomic writes, add file locking if multiple processes can write, and update the failing integration test contract before expanding this bridge.
- Test coverage: Local persistence tests exist in `tests/infra/test_openmemory.py`, but the repository documents end-to-end failures in `tests/README.md`.

**Runtime update/install flows:**
- Files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/gateway/adapters/mcp_server.py`, `tests/gateway/test_cli.py`
- Why fragile: Runtime behavior depends on subprocesses, git state, OS service managers, and detached processes; MCP auto-update starts from a daemon thread on startup (`src/atelier/gateway/adapters/mcp_server.py:2742-2743`), while the systemd unit generated by `src/atelier/gateway/adapters/cli.py:5884-5899` enables `servicectl run --auto-update`.
- Safe modification: Isolate update logic behind a single service module and cover it with explicit fake-git integration tests before changing restart behavior.
- Test coverage: `tests/gateway/test_cli.py:289-551` exercises parts of `servicectl`, but no direct tests were detected for `src/atelier/gateway/adapters/mcp_server.py:_check_auto_update`.

## Scaling Limits

**Primary runtime store:**
- Current capacity: Single SQLite database file plus mirrored filesystem artifacts under the Atelier root (`src/atelier/core/foundation/store.py:328-390`).
- Limit: Write-heavy workloads and concurrent workers bottleneck on SQLite locking, especially around jobs (`src/atelier/core/foundation/store.py:1175-1205`) and analytics routes that query raw payload JSON in SQL (`src/atelier/core/service/api.py:2742-2763`, `src/atelier/core/service/api.py:3297-3305`).
- Scaling path: Finish the Postgres storage path end-to-end and stop reaching around the store abstraction from `src/atelier/core/service/api.py`.

## Dependencies at Risk

**psycopg / Postgres support path:**
- Risk: The optional Postgres backend exists, but repository docs already record `test_postgres_store.py` as errored (`tests/README.md:40-42`), and the service layer still assumes SQLite (`src/atelier/core/service/api.py`).
- Impact: Future plans that assume production-ready Postgres support can fail at the API layer even if the store itself works.
- Migration plan: Treat Postgres as incomplete until `create_app()` and analytics/session lookup routes stop using `ContextStore` + `sqlite3` directly.

## Missing Critical Features

**Packaged rubrics loader is stubbed out:**
- Problem: `load_packaged_rubrics()` returns an empty list with a TODO instead of loading bundled rubric files (`src/atelier/core/foundation/rubric_gate.py:83-86`).
- Blocks: Any code path that expects packaged rubric discovery through `atelier.core.rubrics` receives no bundled rubrics by default.

## Test Coverage Gaps

**Gateway/integration test suite drift:**
- What's not tested: Current gateway/install/docs surfaces are not in a trustworthy green state; the repository’s own test inventory reports 96 failures and 43 errors in gateway tests (`tests/README.md:56-82`), including install artifacts, docs, missing specs, and security test failures.
- Files: `tests/README.md`, `tests/gateway/`
- Risk: Refactors in CLI, host installation, docs-linked behavior, and security checks can regress without a reliable CI signal.
- Priority: High

**Stop-hook and OpenMemory end-to-end behavior:**
- What's not tested: `tests/README.md:134-140` records failing OpenMemory integration tests and stop-hook file-state errors.
- Files: `tests/README.md`, `tests/infra/`
- Risk: Runtime shutdown/state persistence and optional memory integration remain brittle under real file-system conditions.
- Priority: Medium

---

*Concerns audit: 2026-05-18*
