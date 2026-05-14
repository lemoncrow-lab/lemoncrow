# Changelog

## Unreleased

### MCP Surface Consolidation (breaking)

Reduced from 26 active MCP tools to 11. No features removed.

**Renamed (atelier\_ prefix dropped; server name `atelier` provides namespace):**

- `atelier_get_reasoning_context` → `context`
- `atelier_rescue_failure` → `rescue`
- `atelier_record_trace` → `trace`
- `atelier_run_rubric_gate` → `verify`
- `atelier_smart_read` → `read`
- `atelier_smart_search` → `search`
- `atelier_smart_edit` → `edit`
- `atelier_route` → `route`
- `atelier_compact` → `compact`
- `atelier_memory` → `memory`

**Consolidated:**

- `atelier_route_decide` + `atelier_route_verify` → `route` (op-dispatch)
- `atelier_compact_tool_output` + `atelier_compress_context` + `atelier_compact_advise` → `compact` (op-dispatch)
- `atelier_memory_upsert_block` + `_get_block` + `_archive` + `_recall` + `_summary` → `memory` (op-dispatch)
- `atelier_search_read` folded into `search` (`mode=chunks`)
- `atelier_repo_map` folded into `search` (`seed_files` arg, `mode=map`)

**Moved to CLI (no longer agent-callable via MCP):**

- `atelier_lesson_inbox`/`decide` → `atelier lesson ...`
- `atelier_consolidation_inbox`/`decide` → `atelier consolidation ...`
- `atelier_report` → `atelier report`
- `atelier_sql_inspect` → `atelier sql inspect`
- `atelier_proof_report` → `atelier proof run/show`
- `atelier_route_contract` → `atelier route contract <host>`

**Removed phantom names from docs:**
`atelier_smart_bash`, `atelier_tool_supervisor`, `atelier_cached_grep`,
`atelier_monitor_event`, `atelier_get_run_ledger`, `atelier_update_run_ledger`,
`atelier_get_environment`, `atelier_get_environment_context`.

- Added the V2 to V3 migration guide and deprecation matrix for operators moving to the hardened memory, benchmark, and context-compression behavior.
- Replaced legacy context-savings claims with the honest V3 replay benchmark and published the measured CSV results.

References:

- [V2 to V3 migration guide](docs/migrations/v2-to-v3.md)
- [V2 to V3 deprecation matrix](docs/migrations/v2-to-v3-deprecation-matrix.md)
- [V3 honest savings benchmark](docs/benchmarks/v3-honest-savings.md)
