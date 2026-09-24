"""MCP server (stdio JSON-RPC) for the LemonCrow context runtime.

Implements a minimal subset of the Model Context Protocol sufficient for
Codex / Claude Code to discover and call the runtime tools.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from lemoncrow_client.kit.contract_guard import detect_weakening as _kit_detect_weakening
from lemoncrow_client.kit.contract_guard import guard_enabled as _test_contract_guard_enabled
from lemoncrow_client.kit.edit import normalize_edit_aliases as _kit_normalize_edit_aliases
from lemoncrow_client.kit.edit import require_content as _kit_require_content
from lemoncrow_client.kit.fsio import FileSnapshot
from lemoncrow_client.kit.fsio import restore as _restore_snapshots
from lemoncrow_client.kit.fsio import snapshot as _snapshot_paths
from lemoncrow_client.kit.read_path import (
    split_file_opts as _split_file_opts,
)
from lemoncrow_client.kit.read_path import (
    split_read_range_suffix as _split_read_range_suffix,
)
from lemoncrow_client.kit.redaction import redact
from lemoncrow_client.kit.search import count_grep_hits
from pydantic import Field

from lemoncrow import __version__ as lemoncrow_version
from lemoncrow.core.capabilities.default_definitions import DefaultRegistry, build_default_registry
from lemoncrow.core.capabilities.host_runners import resolve_swarm_runner_command
from lemoncrow.core.capabilities.model_settings import normalize_model_for_host, resolve_host_model
from lemoncrow.core.capabilities.workflow_runtime_state import (
    coerce_workflow_review_decision as _coerce_workflow_review_decision,
)
from lemoncrow.core.capabilities.workflow_runtime_state import (
    pause_workflow_runtime as _pause_workflow_runtime,
)
from lemoncrow.core.capabilities.workflow_runtime_state import (
    require_active_workflow_runtime as _require_active_workflow_runtime,
)
from lemoncrow.core.capabilities.workflow_runtime_state import (
    stop_workflow_runtime as _stop_workflow_runtime,
)
from lemoncrow.core.capabilities.workflow_runtime_state import (
    workflow_runtime_state as _workflow_runtime_state,
)
from lemoncrow.core.capabilities.workflow_runtime_state import (
    workflow_runtime_status as _coerce_workflow_runtime_status,
)
from lemoncrow.core.capabilities.workflow_runtime_state import (
    write_workflow_runtime_state as _write_workflow_runtime_state,
)
from lemoncrow.core.environment import (
    mcp_tool_visible_to_llm,
)
from lemoncrow.gateway.adapters.mcp import ledger as _ledger
from lemoncrow.gateway.adapters.mcp.bash import (  # noqa: F401  (registers bash tool + re-exports)
    _BASH_STATS_MAX_KEYS,
    _BASH_STATS_PRUNE_TO,
    _DEFAULT_BASH_SOFT_TIMEOUT,
    BASH_TOOL_INPUT_SCHEMA,
    _bash_command_key,
    _bash_omitted_tokens_saved,
    _record_bash_command_stats,
    _render_bash_text,
    _run_bash_tool,
)
from lemoncrow.gateway.adapters.mcp.bash import (
    tool_bash as tool_bash,
)
from lemoncrow.gateway.adapters.mcp.control import handle_control_request
from lemoncrow.gateway.adapters.mcp.deferral import (  # noqa: F401  (re-exported for back-compat)
    _defer_bash_enabled,
    _defer_web_fetch_enabled,
    _deferral_context,
    _deferral_supported,
    _deferred_completion_executor,
    _DeferredResult,
)
from lemoncrow.gateway.adapters.mcp.deferral import (
    _Deferred as _Deferred,
)
from lemoncrow.gateway.adapters.mcp.errors import (
    tool_error_code as _tool_error_code,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.errors import (
    tool_exception_response,
)
from lemoncrow.gateway.adapters.mcp.framework import (  # noqa: F401  (re-exported for back-compat)
    _COERCE_UNCHANGED,
    _annotation_base_types,
    _coerce_json_strings,
    _coerce_str_to_annotation,
    _slim_schema,
    _ToolArgumentError,
    mcp_tool,
)
from lemoncrow.gateway.adapters.mcp.framework import (
    TOOLS as TOOLS,
)
from lemoncrow.gateway.adapters.mcp.fs_access import (  # noqa: F401  (re-exported for back-compat)
    _CLAUDE_ADDITIONAL_DIRS_CACHE,
    _claude_additional_dirs,
)
from lemoncrow.gateway.adapters.mcp.jsonrpc import error as _err
from lemoncrow.gateway.adapters.mcp.jsonrpc import ok as _ok
from lemoncrow.gateway.adapters.mcp.ledger import (  # noqa: F401  (re-exported for back-compat)
    _MAX_HTTP_SESSION_LEDGERS,
    _detect_agent,
    _get_claude_session_id,
    _get_product_session_id,
    _get_realtime_context,
    _http_session_ledgers,
    _http_session_ledgers_lock,
    _ledger_for_session,
    _mcp_window_id,
    _record_full_read,
    _request_ledger,
    _request_session_identity,
    _request_session_model,
    _resolve_live_session_id,
    _workspace_ws_hash,
)
from lemoncrow.gateway.adapters.mcp.ledger import (
    _clear_request_ledger as _clear_request_ledger,
)
from lemoncrow.gateway.adapters.mcp.ledger import (
    _clear_request_session as _clear_request_session,
)
from lemoncrow.gateway.adapters.mcp.ledger import (
    _get_ledger as _get_ledger,
)
from lemoncrow.gateway.adapters.mcp.ledger import (
    _set_request_ledger as _set_request_ledger,
)
from lemoncrow.gateway.adapters.mcp.ledger import (
    _set_request_session as _set_request_session,
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    DIGEST_WIDTH as _DIGEST_WIDTH,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    LINE_DIGEST as _LINE_DIGEST,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    MAX_RANGE_READ_SIG_PATHS as _MAX_RANGE_READ_SIG_PATHS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    MAX_RANGE_READ_SIG_SESSIONS as _MAX_RANGE_READ_SIG_SESSIONS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    MAX_SIG_DIGEST_BYTES as _MAX_SIG_DIGEST_BYTES,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    RANGE_READ_SIGS as _RANGE_READ_SIGS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    RELOCATE_CONTEXT_RADII as _RELOCATE_CONTEXT_RADII,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    aligned_occurrences as _aligned_occurrences,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    line_digests as _line_digests,
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    range_read_sigs as _range_read_sigs,
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    range_read_sigs_lock as _range_read_sigs_lock,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    record_read_sig as _record_read_sig,
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    relocate_served_range as _relocate_served_range,
)
from lemoncrow.gateway.adapters.mcp.read_state import (
    retarget_range_edit as _retarget_range_edit,
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    HOST_SESSION_ENVS as _HOST_SESSION_ENVS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    get_mcp_model as _get_mcp_model,
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    read_workspace_session_bridge as _read_workspace_session_bridge,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    resolved_host_session as _resolved_host_session,
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    resolved_host_session_id as _resolved_host_session_id,
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    workspace_bridge_file as _workspace_bridge_file,
)
from lemoncrow.gateway.adapters.mcp.session_identity import (
    workspace_bridge_session_id as _workspace_bridge_session_id,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.session_state import (  # noqa: F401  (re-exported for back-compat)
    _MCP_ID,
    _MCP_SESSION_FILE_LOCK,
    _forget_mcp_managed_bash,
    _lemoncrow_root,
    _mcp_session_file,
    _mutate_mcp_managed_bash,
    _record_mcp_managed_bash,
)
from lemoncrow.gateway.adapters.mcp.smart_state import (  # noqa: F401  (re-exported for back-compat)
    _STATE_LOCK,
    _acquire_smart_state_flock,
    _read_smart_state,
    _release_smart_state_flock,
    _smart_state_path,
    _write_smart_state,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    CodeIntelHandlerHooks,
    configure_code_intel_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    tool_blame as tool_blame,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    tool_cache as tool_cache,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    tool_graph as tool_graph,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    tool_index as tool_index,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    tool_pattern as tool_pattern,
)
from lemoncrow.gateway.adapters.mcp.tools_code_intel import (
    tool_relations as tool_relations,
)
from lemoncrow.gateway.adapters.mcp.tools_commodity import (  # noqa: F401  (registers commodity tools + re-exports)
    tool_mcp,
    tool_web_fetch,
)
from lemoncrow.gateway.adapters.mcp.tools_compact import (
    CompactHandlerHooks,
    configure_compact_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_compact import (
    tool_compact as tool_compact,
)
from lemoncrow.gateway.adapters.mcp.tools_context import (
    ContextHandlerHooks,
    configure_context_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_context import (
    tool_get_context as tool_get_context,
)
from lemoncrow.gateway.adapters.mcp.tools_edit import (
    EDIT_TOOL_INPUT_SCHEMA as EDIT_TOOL_INPUT_SCHEMA,
)
from lemoncrow.gateway.adapters.mcp.tools_edit import (
    EditHandlerHooks,
    configure_edit_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_edit import (
    recover_edit_args as _lift_flattened_edit_args,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.tools_edit import (
    tool_smart_edit as tool_smart_edit,
)
from lemoncrow.gateway.adapters.mcp.tools_memory import (
    MemoryHandlerHooks,
    configure_memory_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_memory import (
    tool_memory as tool_memory,
)
from lemoncrow.gateway.adapters.mcp.tools_orchestration import (
    WORKFLOW_TOOL_INPUT_SCHEMA as WORKFLOW_TOOL_INPUT_SCHEMA,
)
from lemoncrow.gateway.adapters.mcp.tools_orchestration import (
    OrchestrationHandlerHooks,
    configure_orchestration_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_orchestration import (
    tool_agent as tool_agent,
)
from lemoncrow.gateway.adapters.mcp.tools_orchestration import (
    tool_workflow as tool_workflow,
)
from lemoncrow.gateway.adapters.mcp.tools_read import (
    ReadHandlerHooks,
    configure_read_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_read import (
    recover_read_stray_query as _recover_read_stray_query,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.tools_read import (
    tool_smart_read as tool_smart_read,
)
from lemoncrow.gateway.adapters.mcp.tools_rescue import (
    RescueHandlerHooks,
    configure_rescue_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_rescue import (
    tool_rescue_failure as tool_rescue_failure,
)
from lemoncrow.gateway.adapters.mcp.tools_review import (
    ReviewHandlerHooks,
    configure_review_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_review import (
    tool_review_evidence as tool_review_evidence,
)
from lemoncrow.gateway.adapters.mcp.tools_review import (
    tool_review_feedback_addressed as tool_review_feedback_addressed,
)
from lemoncrow.gateway.adapters.mcp.tools_review import (
    tool_review_rationale as tool_review_rationale,
)
from lemoncrow.gateway.adapters.mcp.tools_search import (
    SearchHandlerHooks,
    configure_search_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_search import (
    scope_search_matches_to_range as _scope_search_matches_to_range,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.adapters.mcp.tools_search import (
    tool_code_search as tool_code_search,
)
from lemoncrow.gateway.adapters.mcp.tools_search import (
    tool_grep as tool_grep,
)
from lemoncrow.gateway.adapters.mcp.tools_search import (
    tool_smart_search as tool_smart_search,
)
from lemoncrow.gateway.adapters.mcp.tools_statusline import (
    StatuslineHandlerHooks,
    configure_statusline_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_statusline import (
    tool_statusline_segment as tool_statusline_segment,
)
from lemoncrow.gateway.adapters.mcp.tools_trace import (
    MAX_TRACE_FILE_BYTES as _MAX_TRACE_FILE_BYTES,
)
from lemoncrow.gateway.adapters.mcp.tools_trace import (
    MAX_TRACE_FILES as _MAX_TRACE_FILES,
)
from lemoncrow.gateway.adapters.mcp.tools_trace import (
    TraceHandlerHooks,
    configure_trace_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_trace import (
    tool_record_trace as tool_record_trace,
)
from lemoncrow.gateway.adapters.mcp.tools_utility import (
    SQL_TOOL_INPUT_SCHEMA as SQL_TOOL_INPUT_SCHEMA,
)
from lemoncrow.gateway.adapters.mcp.tools_utility import (
    tool_orient as tool_orient,
)
from lemoncrow.gateway.adapters.mcp.tools_utility import (
    tool_scan as tool_scan,
)
from lemoncrow.gateway.adapters.mcp.tools_utility import (
    tool_sql as tool_sql,
)
from lemoncrow.gateway.adapters.mcp.tools_verify import (
    VerifyHandlerHooks,
    configure_verify_handler_hooks,
)
from lemoncrow.gateway.adapters.mcp.tools_verify import (
    tool_run_rubric_gate as tool_run_rubric_gate,
)
from lemoncrow.gateway.tools.accounting import (
    ToolAccountingHooks,
)
from lemoncrow.gateway.tools.accounting import (
    process_tool_accounting as _canonical_process_tool_accounting,
)
from lemoncrow.gateway.tools.broker import invoke_tool_broker
from lemoncrow.gateway.tools.call_runtime import (
    ToolCallRuntimeHooks,
    configure_default_tool_runtime,
    execute_default_tool_payload,
    finalize_tool_payload,
)
from lemoncrow.gateway.tools.call_runtime import (
    run_tool_call as _canonical_run_tool_call,
)
from lemoncrow.gateway.tools.code_context import (
    code_context_engine as _canonical_code_context_engine,
)
from lemoncrow.gateway.tools.code_context import (
    code_engine_cache as _code_engine_cache,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.code_context import (
    code_engine_cache_limit as _canonical_code_engine_cache_limit,
)
from lemoncrow.gateway.tools.code_context import (
    code_engine_cache_lock as _code_engine_cache_lock,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.code_context import (
    code_engine_for_current_call as _code_engine_for_current_call,
)
from lemoncrow.gateway.tools.code_context import (
    reset_code_context_cache as _reset_code_context_cache,
)
from lemoncrow.gateway.tools.code_context import (
    scoped_context_cache as _scoped_context_cache,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.code_context import (
    scoped_context_cache_lock as _scoped_context_cache_lock,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.code_context import (
    scoped_context_capability as _canonical_scoped_context_capability,
)
from lemoncrow.gateway.tools.code_context import (
    workspace_code_router as _canonical_workspace_code_router,
)
from lemoncrow.gateway.tools.dedup import (
    MCP_DEDUP_TOOLS as _DEDUP_TOOLS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.dedup import (
    read_dedup_resource as _read_dedup_resource,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.errors import (
    ToolProtocolError,
)
from lemoncrow.gateway.tools.execution import (
    route_enforcement_enabled as _route_enforcement_enabled,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.invocation import (
    REMOTE_TOOLS as _REMOTE_TOOLS,
)
from lemoncrow.gateway.tools.lifecycle import (
    ToolCallLifecycle as _ToolCallLifecycle,
)
from lemoncrow.gateway.tools.lifecycle import (
    ToolLifecycleHooks,
)
from lemoncrow.gateway.tools.loop_review import (
    LOOP_TRACKER_LOCK as _LOOP_TRACKER_LOCK,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.loop_review import (
    MAX_LOOP_TRACKER_SESSIONS as _MAX_LOOP_TRACKER_SESSIONS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.loop_review import (
    loop_nudge_for_call as _canonical_loop_nudge_for_call,
)
from lemoncrow.gateway.tools.loop_review import (
    loop_review_enabled as _loop_review_enabled,
)
from lemoncrow.gateway.tools.loop_review import (
    loop_tracker_sessions as _loop_tracker_sessions,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.memory import (
    archive_memory as _archive_memory_impl,
)
from lemoncrow.gateway.tools.memory import (
    get_memory_block as _get_memory_block_impl,
)
from lemoncrow.gateway.tools.memory import (
    recall_memory as _recall_memory_impl,
)
from lemoncrow.gateway.tools.memory import (
    session_recall_passages as _session_recall_passages_impl,
)
from lemoncrow.gateway.tools.memory import (
    store_memory_fact as _store_memory_fact_impl,
)
from lemoncrow.gateway.tools.memory import (
    vote_memory_fact as _vote_memory_fact_impl,
)
from lemoncrow.gateway.tools.model_routing import (
    ModelRoutingHooks,
)
from lemoncrow.gateway.tools.model_routing import (
    finalize_model_recommendation as _canonical_finalize_model_recommendation,
)
from lemoncrow.gateway.tools.model_routing import (
    model_recommendation_state as _canonical_model_recommendation_state,
)
from lemoncrow.gateway.tools.model_routing import (
    persist_legacy_route as _canonical_persist_legacy_route,
)
from lemoncrow.gateway.tools.model_routing import (
    prepare_model_recommendation as _canonical_prepare_model_recommendation,
)
from lemoncrow.gateway.tools.model_routing import (
    workflow_state_from_workspace as _canonical_workflow_state_from_workspace,
)
from lemoncrow.gateway.tools.output import (
    HOST_INLINE_RESULT_CHARS as _HOST_INLINE_RESULT_CHARS,
)
from lemoncrow.gateway.tools.output import (
    MAX_WIRE_BYTES as _MAX_WIRE_BYTES,
)
from lemoncrow.gateway.tools.output import (
    SPILL_CHAR_CAP_TOOLS as _SPILL_CHAR_CAP_TOOLS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    SPILL_TOOLS as _SPILL_TOOLS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    auto_compact_result_text as _auto_compact_result_text,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    compact_result_chars as _compact_result_chars,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    compact_result_text as _compact_result_text,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    effective_spill_tool as _effective_spill_tool,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    max_result_bytes as _max_result_bytes,
)
from lemoncrow.gateway.tools.output import (
    output_spill_enabled as _tool_output_spill_enabled,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    spill_oversized_result_text as _spill_oversized_result_text,
)
from lemoncrow.gateway.tools.output import (
    spill_result_chars as _spill_result_chars,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    trimmed_tokens_saved as _trimmed_tokens_saved,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.output import (
    truncate_result_text as _truncate_result_text,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    ARCHIVE_MEDIA_TYPES as _ARCHIVE_MEDIA_TYPES,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    ARCHIVE_SUFFIXES as _ARCHIVE_SUFFIXES,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    DEFAULT_READ_BATCH_BUDGET_BYTES as _DEFAULT_READ_BATCH_BUDGET_BYTES,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    DEFAULT_READ_INLINE_BUDGET_BYTES as _DEFAULT_READ_INLINE_BUDGET_BYTES,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    MAX_INLINE_IMAGE_BYTES as _MAX_INLINE_IMAGE_BYTES,
)
from lemoncrow.gateway.tools.read_policy import (
    READ_SUGGEST_PRUNE_DIRS as _READ_SUGGEST_PRUNE_DIRS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    SUMMARY_TARGET_CHARS as _SUMMARY_TARGET_CHARS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    apply_batch_read_budget as _canonical_apply_batch_read_budget,
)
from lemoncrow.gateway.tools.read_policy import (
    batch_entry_bytes as _batch_entry_bytes,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    binary_read_message as _binary_read_message,
)
from lemoncrow.gateway.tools.read_policy import (
    read_batch_budget_bytes as _read_batch_budget_bytes,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.read_policy import (
    read_inline_budget_bytes as _read_inline_budget_bytes,
)
from lemoncrow.gateway.tools.read_policy import (
    read_summary_response as _canonical_read_summary_response,
)
from lemoncrow.gateway.tools.read_policy import (
    suggest_paths_for_missing as _suggest_paths_for_missing,
)
from lemoncrow.gateway.tools.rendering import (  # noqa: F401  (compat re-exports)
    _GUTTER_ANCHOR_EVERY,
    _READ_OUTLINE_MAX_LINES,
    _SECTION_GUTTER_RE,
    _append_search_verdict_footer,
    _compress_candidate_files,
    _render_code_search_md,
    _render_compact_md,
    _render_context_tool_md,
    _render_memory_md,
    _render_read_md,
    _render_read_outline_md,
    _render_rescue_md,
    _render_search_md,
    _render_symbol_read_md,
    _render_verify_md,
    _sparse_gutter,
)
from lemoncrow.gateway.tools.rendering import (
    CODE_INTEL_RENDER_TOOLS as _CODE_INTEL_TOOLS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.rendering import (
    render_tool_result_text as _canonical_render_tool_result_text,
)
from lemoncrow.gateway.tools.routing import (
    TASK_TEXT_KEYS as _TASK_TEXT_KEYS,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.routing import (
    normalize_model_id as _normalize_model_id,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.routing import (
    provider_for_model as _provider_for_model,
)
from lemoncrow.gateway.tools.routing import (
    restore_legacy_route as _restore_legacy_route,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.routing import (
    route_outcome_calibration as _route_outcome_calibration,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.routing import (
    task_text_from_args as _task_text_from_args,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.savings import (
    coerce_saved_tokens as _coerce_saved_tokens,
)
from lemoncrow.gateway.tools.savings import (
    extract_compact_output_tokens_saved as _extract_compact_output_tokens_saved,
)
from lemoncrow.gateway.tools.savings import (
    extract_tokens_saved as _extract_tokens_saved,
)
from lemoncrow.gateway.tools.semantic_memory import (
    semantic_file_memory as _semantic_file_memory,
)
from lemoncrow.gateway.tools.semantic_memory import (
    semantic_file_memory_cache as _semantic_file_memory_cache,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.semantic_memory import (
    semantic_file_memory_lock as _semantic_file_memory_lock,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.state import (
    NO_CODE_ENGINE_OVERRIDE as _NO_CODE_ENGINE_OVERRIDE,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.state import (
    clear_request_code_context_engine as _clear_request_code_context_engine,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.state import (
    request_code_engine_override as _request_code_engine_override,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.state import (
    set_request_code_context_engine as _set_request_code_context_engine,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.state import (
    tool_call_counterfactual as _tool_call_counterfactual,
)
from lemoncrow.gateway.tools.state import (
    tool_call_images as _tool_call_images,
)
from lemoncrow.gateway.tools.state import (
    tool_call_raw_result as _tool_call_raw_result,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.state import (
    tool_call_rendered_text as _tool_call_rendered_text,
)
from lemoncrow.gateway.tools.state import (
    tool_call_tokens_saved as _tool_call_tokens_saved,
)
from lemoncrow.gateway.tools.surface import CORE_MCP_TOOLS as _CORE_MCP_TOOLS  # noqa: F401  (compat re-export)
from lemoncrow.gateway.tools.surface import (
    MCP_PROTOCOL_VERSION as PROTOCOL_VERSION,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.surface import (
    SERVER_DISPLAY_NAME,  # noqa: F401  (compat re-export)
    SERVER_INSTRUCTIONS,  # noqa: F401  (compat re-export)
    SERVER_NAME,
    SERVER_VERSION,
    TOOL_BROKER_SURFACE_SPEC,
)
from lemoncrow.gateway.tools.surface import (
    mcp_tool_profile as _mcp_tool_profile,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.surface import (
    tool_description as _tool_description,
)
from lemoncrow.gateway.tools.surface import (
    tool_profile_exposes as _tool_profile_exposes,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    clear_request_project as _clear_request_project,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    extract_request_project as _extract_request_project,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    http_project_override_allowed as _http_project_override_allowed,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    is_within_root as _is_within_root,
)
from lemoncrow.gateway.tools.workspace import (
    next_session_cwd as _next_session_cwd,
)
from lemoncrow.gateway.tools.workspace import (
    project_override_root as _project_override_root,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    request_project as _request_project,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    session_worktree_root as _canonical_session_worktree_root,
)
from lemoncrow.gateway.tools.workspace import (
    set_request_project as _set_request_project,  # noqa: F401  (compat re-export)
)
from lemoncrow.gateway.tools.workspace import (
    workspace_path as _workspace_path,
)
from lemoncrow.gateway.tools.workspace import (
    workspace_root as _workspace_root,
)
from lemoncrow.infra.runtime.run_ledger import (
    RunLedger,
    outcomes_path,
)
from lemoncrow.pro.capabilities.code_context.diversity import demote_doc_overflow, query_wants_docs
from lemoncrow.pro.capabilities.code_context.evidence_resolution import (
    EvidenceResolutionAction,
    propose_evidence_resolution,
)
from lemoncrow.pro.capabilities.code_context.evidence_state import evaluate_explore_evidence
from lemoncrow.pro.capabilities.code_context.search_feedback import SearchFeedbackPolicy
from lemoncrow.pro.capabilities.memory.redaction import redact_memory_input as _redact_memory_input
from lemoncrow.pro.capabilities.memory.runtime import memory_service, memory_store
from lemoncrow.pro.capabilities.owned_execution_lanes import (
    OwnedExecutionError,
    execute_owned_prompt,
)
from lemoncrow.pro.capabilities.owned_execution_routing import (
    OwnedCachePolicy,
    OwnedRouteRequest,
    select_owned_route,
)

if TYPE_CHECKING:
    from lemoncrow.gateway.adapters.runtime import ContextRuntime
    from lemoncrow.pro.capabilities.archival_recall import ArchivalRecallCapability
    from lemoncrow.pro.capabilities.memory import MemoryService

logger = logging.getLogger(__name__)


def _version_key(version: str) -> tuple[int, ...]:
    """Dotted version -> comparable int tuple (non-numeric chunks count as 0)."""
    parts: list[int] = []
    for chunk in version.split("."):
        match = re.match(r"\d+", chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def _warm_pricing_table() -> None:
    """Pre-build the LiteLLM-backed pricing table off the response path.

    The first tools/call prepares a model recommendation
    (``_prepare_model_recommendation`` -> ``select_owned_route``), which builds
    the counterfactual routing pricing table: a full parse of the LiteLLM
    catalog plus ``pricing.yaml`` overrides (~100-200ms of pure-Python work).
    Warming it on a daemon thread keeps that parse out of the first tool
    response.  Cache semantics are unchanged — the per-process ``lru_cache``
    and its ``override_pricing()`` invalidation still apply; this only moves
    when the first build happens.  Fail-open.
    """
    try:
        from lemoncrow.pro.capabilities.counterfactual.pricing import load_pricing_table

        load_pricing_table()
    except Exception:
        logger.debug("pricing table pre-warm failed", exc_info=True)


# Spawned at import (top of module, so the parse overlaps the rest of this
# module's own import work) rather than in main(), so every entry point that
# dispatches through _handle (stdio serve, HTTP adapter, SDK/embedded direct
# dispatch) gets the warm start before its first tool response.  The
# counterfactual pricing module is already fully imported at this point (see
# the owned_execution_routing import above), so the thread never contends on
# import locks.
threading.Thread(target=_warm_pricing_table, name="lemoncrow-pricing-warm", daemon=True).start()

# Tool-surface identity and presentation policy now live outside the MCP transport.
# These aliases plus the visibility wrapper remain for compatibility while
# callers migrate to ``lemoncrow.gateway.tools.surface``.

CONTEXT_WINDOW_TOKENS = 200_000
COMPACT_ADVISORY_THRESHOLD = 60.0
AUTO_COMPACT_THRESHOLD = 80.0
HANDOVER_THRESHOLD = 95.0
AUTO_COMPACT_MIN_TURNS = 15
# Bypass the min-turns gate when utilisation already exceeds this level —
# a few very large turns can fill the window just as fast as many small ones.
AUTO_COMPACT_HIGH_UTIL_OVERRIDE = 90.0


def _tool_visible_to_llm(tool_name: str, spec: dict[str, Any]) -> bool:
    # Keep this wrapper local for backwards-compatible monkey-patching of
    # ``mcp_server.mcp_tool_visible_to_llm`` in embedded/test callers.
    del spec
    return mcp_tool_visible_to_llm(tool_name)


# _COERCE_UNCHANGED, the argument-coercion helpers, _ToolArgumentError and the
# @mcp_tool decorator now live in mcp.framework (imported above).


# G13 — caller-selectable output encoding for read/search/grep. NOT published to
# LLMs (auto already picks the optimal encoding); kept as a power/CLI/benchmark
# knob, accepted by the handler but stripped from the advertised schema. Defined
# before the tool handlers so the @mcp_tool decorator can resolve the Annotated
# default at import time. The handler ignores this arg; the MCP dispatcher reads
# `args["format"]` and applies the N6-gated N7 columnar encoding. `auto`
# (default) keeps today's byte-compatible output.
_FORMAT_FIELD = Field(
    default="auto",
    description=(
        "Output encoding: auto (default, unchanged), json (force raw JSON), or compact (N6-gated columnar encoding)."
    ),
)


# --------------------------------------------------------------------------- #
# session_state.json helpers                                                  #
# --------------------------------------------------------------------------- #

# moved to mcp.ledger (imported/re-exported near the top).
# Per-request ledger override for the HTTP transport: set on the worker thread
# that runs _handle so concurrent HTTP clients each accumulate their own run.json
# instead of co-mingling into the process-global _current_ledger.
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
_product_session_started_at: float | None = None
_last_plan_hash_by_session: dict[str, str] = {}
_last_plan_by_session: dict[str, dict[str, Any]] = {}
_last_blocked_plan_hash_by_session: dict[str, str] = {}

# --------------------------------------------------------------------------- #
# Trajectory monitor state (per session)                                      #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class _MonitorSession:
    """Per-session DifficultyFSM + step history for trajectory monitoring."""

    fsm: Any = dataclasses.field(default=None)
    steps: list[str] = dataclasses.field(default_factory=list)
    composite: float = 0.0
    _call_count: int = 0

    def __post_init__(self) -> None:
        if self.fsm is None:
            from lemoncrow.pro.capabilities.monitors.fsm import DifficultyFSM

            self.fsm = DifficultyFSM()


_monitor_sessions: dict[str, _MonitorSession] = {}
_MAX_MONITOR_STEPS = 25
_MAX_MONITOR_SESSIONS = 64
# Serializes mutation of the shared _monitor_sessions map / _MonitorSession
# state: the dispatcher runs context calls on a thread pool, so two concurrent
# calls for one session must not race the setdefault/eviction/steps mutation.
_MONITOR_LOCK = threading.Lock()


def _advance_monitors(session_id: str, task: str, original_task: str) -> tuple[float, bool]:
    """Advance per-session trajectory monitors; return (composite, skip_etraces).

    Guards itself behind the bench kill-switch so monitors don't interfere with
    benchmark runs.  Runs ``evaluate_all`` once every ``monitor_cooldown_steps``
    calls (as determined by the FSM state) to amortise the regex cost.
    """
    try:
        from lemoncrow.bench.mode import is_off as _bench_is_off

        if _bench_is_off():
            return 0.0, False

        from lemoncrow.pro.capabilities.monitors import evaluate_all
        from lemoncrow.pro.capabilities.monitors.fsm import score_step

        with _MONITOR_LOCK:
            ms = _monitor_sessions.setdefault(session_id, _MonitorSession())
            if len(_monitor_sessions) > _MAX_MONITOR_SESSIONS:
                # Bound the session map: a marathon process seeing many session
                # ids must not leak _MonitorSession objects. Evict oldest (skip current).
                for _stale in list(_monitor_sessions)[: len(_monitor_sessions) - _MAX_MONITOR_SESSIONS]:
                    if _stale != session_id:
                        _monitor_sessions.pop(_stale, None)
            ms.steps.append(task)
            if len(ms.steps) > _MAX_MONITOR_STEPS:
                ms.steps = ms.steps[-20:]
            ms.fsm.transition(score_step(task))
            ms._call_count += 1
            cooldown = ms.fsm.monitor_cooldown_steps
            run_eval = ms._call_count % cooldown == 0 or ms._call_count == 1
            steps_snapshot = list(ms.steps)
        if run_eval:
            result = evaluate_all(steps_snapshot, task=original_task)
            ms.composite = result.composite
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return 0.0, False

    return ms.composite, ms.fsm.skip_etraces


# LemonCrow-internal MCP process identity — generated once at import, never changes.
# SessionStart hook finds this file and writes the Claude session UUID + model into it.
# _get_claude_session_id() reads it once then caches in _cached_claude_session_id.
# _MCP_ID moved to mcp.session_state (imported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).
# _current_context_state cache: session id -> (stat-signature, (ctx, model)).
# That probe runs on every savings-bearing tool call; without this it re-tails
# and JSON-parses a 64 KB transcript window every time. Keyed on the candidate
# transcripts' (path, mtime_ns, size) so any new turn / rewrite invalidates it.
_CONTEXT_STATE_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], tuple[int, str]]] = {}
_STDOUT_LOCK = threading.Lock()
# _STATE_LOCK moved to mcp.smart_state (imported/re-exported).
# Per-file edit locks: concurrent edit calls (the MCP dispatcher runs a thread
# pool) that touch the same file must not interleave snapshot/apply/write, or one
# write clobbers the other (lost update). _EDIT_PATH_LOCKS maps a resolved file
# path to its Lock; _EDIT_PATH_LOCKS_GUARD serializes registry mutation only.
_EDIT_PATH_LOCKS: dict[str, threading.Lock] = {}
_EDIT_PATH_LOCKS_GUARD = threading.Lock()
# Per-session-id cache for the savings sidecar path; set on first write so the
# path stays stable if the process runs past midnight (date partition fixed at
# session-start time, not re-derived on every call).
_SAVINGS_SIDECAR_PATH_BY_SID: dict[str, Path] = {}
_DEFAULT_MCP_MAX_WORKERS = 16

# --------------------------------------------------------------------------- #
# Search verdict state                                                        #
# --------------------------------------------------------------------------- #
# The policy owns bounded per-session state independent of MCP. Keep the
# histories alias for compatibility with tests/debug surfaces that clear it.
_SEARCH_FEEDBACK_POLICY = SearchFeedbackPolicy(max_sessions=64)
_search_history_sessions = _SEARCH_FEEDBACK_POLICY.histories


def _count_search_hits(payload: dict[str, Any]) -> int:
    """Hit count for a search payload (items pre-view, matches post-view)."""
    for key in ("matches", "items", "ranked_files"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _search_cascade_enabled() -> bool:
    """Phase 3 cascade-on-empty toggle (default on; set 0/false to disable)."""
    return os.environ.get("LEMONCROW_SEARCH_CASCADE_ON_EMPTY", "1").strip().lower() not in {"0", "false", "no", ""}


def _apply_search_verdict(
    result: dict[str, Any],
    *,
    query: str,
    hit_count: int,
    channels: Any | None = None,
) -> dict[str, Any]:
    """Apply transport-neutral search feedback using the live host session id."""

    if not isinstance(result, dict) or not (query or "").strip():
        return result

    from lemoncrow.pro.capabilities.code_context.search_verdict import ChannelHealth

    session_id = _get_claude_session_id() or "_global"
    channel_health = channels if isinstance(channels, ChannelHealth) else ChannelHealth()
    return _SEARCH_FEEDBACK_POLICY.apply(
        result,
        session_id=session_id,
        query=query,
        hit_count=hit_count,
        channels=channel_health,
        decision_sink=_get_ledger(),
    )


def _edit_path_locks(resolved_paths: list[Path]) -> list[threading.Lock]:
    """Return locks for *resolved_paths*, ordered deterministically to avoid
    deadlock when an edit batch touches several files at once."""
    keys = sorted({str(p) for p in resolved_paths})
    with _EDIT_PATH_LOCKS_GUARD:
        return [_EDIT_PATH_LOCKS.setdefault(key, threading.Lock()) for key in keys]


_MAX_MCP_MAX_WORKERS = 64

# Per-session count of partial (range) reads per resolved path. Paging one file
# by hand 3+ times wastes a turn per chunk (and the chunks pile up in context
# anyway), so the third partial read of a path escalates to a full read. Keyed by
# resolved path; a :full read of that path resets it. See _smart_read_single.

# Honest-baseline caps for savings accounting live in gateway.tools.output.
# bash symbols moved to mcp.bash (imported/registered near the top).


def _service_backed_state() -> bool:
    return True


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


def _emit_mcp_session_start() -> None:
    global _product_session_started_at
    if _product_session_started_at is not None:
        return
    _register_mcp_session()  # register LemonCrow MCP ID so SessionStart hook can find us
    from importlib.metadata import PackageNotFoundError, version

    from lemoncrow.core.foundation.identity import get_anon_id, platform_payload
    from lemoncrow.core.service.telemetry import emit_product

    try:
        service_version = version("lemoncrow")
    except PackageNotFoundError:
        service_version = SERVER_VERSION
    # OTel is initialized lazily on first emit_product_log call.
    _product_session_started_at = time.perf_counter()
    emit_product(
        "session_start",
        agent_host=_detect_agent(),
        lemoncrow_version=service_version,
        anon_id=get_anon_id(),
        session_id=_get_product_session_id(),
        **platform_payload(),
    )


def _emit_mcp_session_end(exit_reason: str = "success") -> None:
    if _product_session_started_at is None:
        return
    from lemoncrow.core.service.telemetry import emit_product
    from lemoncrow.core.service.telemetry.schema import bucket_duration_s

    elapsed = max(0.0, time.perf_counter() - _product_session_started_at)
    emit_product(
        "session_end",
        session_id=_get_product_session_id(),
        duration_s_bucket=bucket_duration_s(elapsed),
        exit_reason=exit_reason,
    )


def _match_mcp_lexical(args: dict[str, Any]) -> None:
    from lemoncrow.core.service.telemetry.frustration import match_frustration

    for key in ("task", "query", "user_goal", "error"):
        value = args.get(key)
        if isinstance(value, str):
            match_frustration(value, surface="mcp_prompt", session_id=_get_product_session_id())


def _emit_playbook_retrieved(scored: list[Any], domain: str | None) -> None:
    from lemoncrow.core.service.telemetry import emit_product
    from lemoncrow.core.service.telemetry.schema import hash_identifier

    for rank, item in enumerate(scored, start=1):
        block = getattr(item, "block", None)
        emit_product(
            "playbook_retrieved",
            block_id_hash=hash_identifier(str(getattr(block, "id", ""))),
            domain=str(getattr(block, "domain", domain or "")),
            retrieval_score=float(getattr(item, "score", 0.0)),
            rank=rank,
            session_id=_get_product_session_id(),
        )


# --------------------------------------------------------------------------- #
# Tool implementations                                                        #
# --------------------------------------------------------------------------- #


# _lemoncrow_root moved to mcp.session_state (imported near the top).


def _make_outcome_writer(led: RunLedger) -> Any:
    """Return a FileStateWriter for outcomes alongside the run file, or None."""
    with contextlib.suppress(Exception):
        from lemoncrow.pro.runtime.outcome_capture import FileStateWriter

        root = led._root
        if root is not None:
            return FileStateWriter(outcomes_path(root, led.agent or "claude", led.session_id))
    return None


# --------------------------------------------------------------------------- #
# Zero-config background service                                              #
# --------------------------------------------------------------------------- #


def _detect_default_branch(repo: Path) -> str | None:
    """Detect the remote default branch (main/master) for *repo*."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "show", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("HEAD branch:"):
                branch = stripped.split(":")[-1].strip()
                if branch:
                    return branch
    except Exception:
        logging.exception("Recovered from broad exception handler")
        logger.warning(
            "Suppressed exception in _detect_default_branch",
            exc_info=True,
        )
    # Fallback: try main then master
    for candidate in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{candidate}"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            logging.exception("Recovered from broad exception handler")
            continue
    return None


_log = logging.getLogger("lemoncrow.mcp")


def _installed_cli_version() -> str | None:
    """Return the version reported by the installed ``lc`` executable."""
    from lemoncrow.core.foundation.update_state import installed_cli_version

    return installed_cli_version()


def _check_auto_update() -> None:
    """Check git remote for a newer version and auto-update if found.

    Compares the version in the remote repo's ``pyproject.toml`` against the
    currently installed version.  If they differ, pulls the repo and runs
    the install script.  Logs errors and emits telemetry on failure but
    never blocks the MCP server.

    Opt-in. Set ``LEMONCROW_AUTO_UPDATE=1`` (or true/yes/on) to enable startup
    git-checkout auto-update; it is OFF by default so the runtime never contacts
    the network unless the user asks.
    """
    import re
    import subprocess

    if os.environ.get("LEMONCROW_AUTO_UPDATE", "").strip().lower() not in ("1", "true", "yes", "on"):
        return

    # Dev/source installs (`make dev` writes <root>/.dev_mode) must never
    # auto-update: a git fetch/merge on the live checkout would fight an
    # in-progress refactor and can clobber uncommitted work. Production installs
    # have no marker. Read the marker fresh (not the cached _is_dev_mode()) so
    # the decision tracks the current root.
    try:
        if (_lemoncrow_root() / ".dev_mode").exists():
            _log.debug("auto-update skipped: dev install (.dev_mode present)")
            return
    except OSError:
        pass

    _log.info("checking for auto-update...")

    try:
        # Determine the repo directory
        install_dir = os.environ.get("LEMONCROW_INSTALL_DIR", "")
        if install_dir:
            repo = Path(install_dir)
            _log.debug("repo from LEMONCROW_INSTALL_DIR: %s", repo)
        else:
            repo = Path(__file__).resolve().parents[4]
            _log.debug("repo from file path: %s", repo)

        if not (repo / ".git").exists():
            _log.debug("not a git checkout - skipping auto-update")
            return  # Not a git checkout, nothing to auto-update

        # The MCP server can remain alive across source updates, leaving the
        # imported ``lemoncrow_version`` at the version it started with. Query
        # the installed executable instead.
        local_version = _installed_cli_version() or lemoncrow_version

        # Fetch latest remote info
        _log.info("fetching latest remote refs from origin...")
        result = subprocess.run(
            ["git", "fetch", "--tags", "--prune", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            _log.warning("git fetch exited %d: %s", result.returncode, result.stderr.strip())
            return

        default_branch = _detect_default_branch(repo)
        if default_branch is None:
            _log.warning("could not detect default remote branch")
            return

        # Read remote version from pyproject.toml
        result = subprocess.run(
            ["git", "show", f"origin/{default_branch}:pyproject.toml"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            _log.warning(
                "could not read remote pyproject.toml (exit %d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return

        match = re.search(r'^version\s*=\s*"([^"]+)"', result.stdout, re.MULTILINE)
        if not match:
            _log.warning("could not parse version from remote pyproject.toml")
            return

        remote_version = match.group(1)
        if _version_key(remote_version) <= _version_key(local_version):
            _log.info("remote version is not newer; skipping auto-update")
            return

        # Newer version detected - pull and reinstall
        _log.info("newer version detected - pulling %s/%s ...", default_branch, default_branch)
        subprocess.run(
            ["git", "pull", "--ff-only", "origin", default_branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        install_script = repo / "scripts" / "install.sh"
        if install_script.exists():
            _log.info("running install script...")
            subprocess.run(
                ["bash", str(install_script), "--local"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
            _log.info("auto-update complete")

            # Write update-state so SessionStart hooks can notify the user.
            # The server's imported version may be stale after the install.
            try:
                from lemoncrow.core.foundation.update_state import write_update_state

                write_update_state(
                    previous_version=local_version,
                    current_version=_installed_cli_version() or remote_version,
                    method="git",
                )
            except Exception:
                _log.exception("failed to write update state")
        else:
            _log.warning("install script not found at %s", install_script)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        _log.exception("auto-update failed")
        with contextlib.suppress(Exception):
            from lemoncrow.core.service.telemetry import emit_product

            emit_product(
                "mcp_auto_update_failed",
                current_version=lemoncrow_version,
                session_id=_get_product_session_id(),
            )


def _run_worker_tick_safe(root: Path) -> None:
    """Process up to 20 pending jobs for *root*, then run the periodic
    maintenance duties the retired servicectl daemon used to own (public
    rollup, session import, recall indexing, workspace pruning, block
    consolidation, retention cleanup, optimize automation).  Run in a daemon
    thread -- never blocks the calling MCP tool call, and each duty below is
    independently exception-guarded so one stuck or failing duty cannot
    suppress the others.
    """
    store = None
    try:
        from lemoncrow.core.service.worker import Worker
        from lemoncrow.infra.storage.factory import create_store

        _store = create_store(root)
        _store.init()
        # Only exposed to the maintenance tick below once init has actually
        # succeeded -- otherwise a half-initialized StoreBundle (init raised
        # partway) would get forwarded and the job-enqueue duties would
        # silently no-op against missing tables every tick, indistinguishable
        # from "genuinely nothing to do".
        store = _store
        worker = Worker(store=store)
        for _ in range(20):
            if worker.run_once() is None:
                break
    except Exception:
        logging.exception("Recovered from broad exception handler")
        logger.warning(
            "Suppressed exception in _run_worker_tick_safe",
            exc_info=True,
        )

    # Public savings rollup: no persistent daemon owns this since the
    # servicectl controller was retired (see git history of
    # infra/runtime/servicectl_lifecycle.py), so this per-tick check is now
    # the only thing that ever flushes the daily aggregate to
    # lemoncrow.com/savings. maybe_flush_public_rollup() is a cheap no-op
    # when not due, so ticking it here on every worker pass is fine, and a
    # failure here must never block job draining above.
    try:
        from lemoncrow.core.service.telemetry.public_rollup import maybe_flush_public_rollup

        maybe_flush_public_rollup(root)
    except Exception:
        logger.warning("Suppressed exception flushing public rollup", exc_info=True)

    # Remaining periodic duties the same retired daemon owned: session
    # import, recall indexing, workspace pruning, block consolidation,
    # retention cleanup, optimize automation. See maintenance_tick's module
    # docstring -- lock-guarded (non-blocking: a busy lock just skips this
    # tick) so concurrent worker threads never run it twice at once, and each
    # duty inside is its own no-op-when-not-due check.
    try:
        from lemoncrow.core.service.maintenance_tick import run_maintenance_tick

        tick_result = run_maintenance_tick(root, store=store)
        logger.debug("maintenance_tick: %s", tick_result)
    except Exception:
        logger.warning("Suppressed exception in periodic maintenance tick", exc_info=True)

    # Stale MCP session-registration pruning: also retired-daemon territory,
    # but unconditional every tick (not interval-gated) in the old servicectl
    # code too -- it's a cheap local glob + liveness check (see
    # prune_stale_mcp_sessions's docstring), not a subprocess or network call,
    # so there is no cost to re-checking it as often as the tick runs. An
    # agent host that leaks its `lc mcp` child otherwise leaves a zombie
    # registration file behind for as long as that host process runs; before
    # this it was only reaped as a side effect of a human running `lc mcp
    # list`. (The old tick's OTHER unconditional duty, refreshing
    # hosts/status.json for a Docker-hosted status page, is not restored: the
    # /hosts endpoint that page depended on was retired in the same commit
    # that dropped the writer, and the current /hosts endpoint computes host
    # status live from history instead of reading that file -- there is no
    # reader left to restore this for.)
    try:
        from lemoncrow.gateway.adapters.mcp.session_state import prune_stale_mcp_sessions

        prune_stale_mcp_sessions(root)
    except Exception:
        logger.warning("Suppressed exception pruning stale mcp sessions", exc_info=True)


_last_worker_spawn_time: float = 0.0
_WORKER_SPAWN_THROTTLE_SECS: float = 30.0
# The most recently spawned tick thread, if any. Job draining used to be fast
# enough that the 30s wall-clock throttle alone was enough to avoid overlap;
# now that a tick can legitimately run for several minutes (the
# subprocess-backed maintenance duties -- session import, recall indexing,
# workspace pruning), the throttle window can elapse while the previous tick
# is still running. Checking Thread.is_alive() here -- rather than a manually
# set/cleared flag -- self-corrects if the thread dies unexpectedly or (as in
# several existing tests) its target is mocked out entirely: there is no
# separate flag that a change to _run_worker_tick_safe's body could forget to
# reset, permanently wedging every future spawn.
_worker_tick_thread: threading.Thread | None = None


def _spawn_worker_if_idle(root: Path) -> None:
    """Spawn a worker thread at most once per throttle window, and never while
    a previous tick thread is still running, to avoid thread storms."""
    import time

    global _last_worker_spawn_time, _worker_tick_thread
    # Serialize the check-and-set so two concurrent light-pool callers can't both
    # pass the throttle window and spawn redundant workers. Spawn the thread
    # OUTSIDE the lock so thread creation doesn't run under _STATE_LOCK.
    with _STATE_LOCK:
        if _worker_tick_thread is not None and _worker_tick_thread.is_alive():
            return
        now = time.monotonic()
        if now - _last_worker_spawn_time < _WORKER_SPAWN_THROTTLE_SECS:
            return
        _last_worker_spawn_time = now
        thread = threading.Thread(
            target=_run_worker_tick_safe,
            args=(root,),
            daemon=True,
        )
        _worker_tick_thread = thread
    try:
        thread.start()
    except RuntimeError:
        # Thread creation failed (e.g. OS thread-limit pressure). Don't let the
        # already-advanced throttle clock suppress the next 30s of ticks -- reset
        # it so the next caller can retry the spawn.
        with _STATE_LOCK:
            _last_worker_spawn_time = 0.0
            _worker_tick_thread = None
        logging.exception("Recovered from broad exception handler")


_runtime_cache: ContextRuntime | None = None
_context_budget_recorder: Any = None


def _runtime() -> ContextRuntime:
    global _runtime_cache

    from lemoncrow.gateway.adapters.runtime import ContextRuntime

    with _STATE_LOCK:
        if _runtime_cache is None:
            _runtime_cache = ContextRuntime(_lemoncrow_root())
    return _runtime_cache


def _verify_handler_hooks() -> VerifyHandlerHooks:
    return VerifyHandlerHooks(runtime=_runtime, get_ledger=_get_ledger)


configure_verify_handler_hooks(_verify_handler_hooks)


def _reset_runtime_cache_for_testing() -> None:
    global _product_session_started_at
    global _runtime_cache, _remote_client, _context_budget_recorder
    global _last_worker_spawn_time
    _ledger._current_ledger = None
    _ledger._realtime_ctx = None
    _ledger._product_session_id = None
    _product_session_started_at = None
    _runtime_cache = None
    _remote_client = None
    _context_budget_recorder = None
    _last_worker_spawn_time = 0.0
    _ledger._WINDOW_SID_CACHE = None
    _ledger._MCP_WINDOW_ID = None
    _ledger._MCP_WINDOW_ID_RESOLVED = False
    _last_plan_hash_by_session.clear()
    _last_plan_by_session.clear()
    _CONTEXT_STATE_CACHE.clear()
    _COMPACT_ADVISE_CACHE.clear()
    _last_blocked_plan_hash_by_session.clear()
    _reset_code_context_cache()


def _live_savings_events_path() -> Path:
    return _lemoncrow_root() / "live_savings_events.jsonl"


# Cap the analytics log: full-file readers (audit_export, advisor, dashboard,
# session_report) O(n)-scan it per render, so unbounded growth is a real cost.
_LIVE_SAVINGS_MAX_BYTES = 8 * 1024 * 1024
_live_savings_dir_ready = False
_live_savings_append_count = 0
# Serializes the dir-ready flag, rotation counter, size-check/rotate, and the
# append so concurrent dispatcher-pool threads can't lose an event on rotation.
_LIVE_SAVINGS_LOCK = threading.Lock()


def _append_live_savings_event(event: dict[str, Any]) -> None:
    """Append a routing / compaction analytics event.

    Display savings ride the MCP response's content[].saved field into the
    transcript and are summed from there. This file remains the log for
    audit_export and cross_vendor_routing.advisor only.
    """
    global _live_savings_dir_ready, _live_savings_append_count
    path = _live_savings_events_path()
    line = json.dumps(event, sort_keys=True) + "\n"
    with _LIVE_SAVINGS_LOCK:
        if not _live_savings_dir_ready:
            # mkdir once per process instead of a syscall on every tool call.
            path.parent.mkdir(parents=True, exist_ok=True)
            _live_savings_dir_ready = True
        _live_savings_append_count += 1
        if _live_savings_append_count % 128 == 0:
            # Periodic size-based rotation (keep one prior generation) so the log
            # cannot grow without bound; checked rarely to avoid a per-call stat().
            try:
                if path.exists() and path.stat().st_size > _LIVE_SAVINGS_MAX_BYTES:
                    path.replace(path.parent / (path.name + ".1"))
            except OSError:
                logging.exception("Recovered from broad exception handler")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def _workspace_savings_path() -> Path:
    """Side log for per-session savings on Copilot CLI and other non-Claude hosts."""

    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

    workspace = os.environ.get("LEMONCROW_WORKSPACE_ROOT") or os.getcwd()
    return resolve_workspace_store_dir(workspace_root=Path(workspace)) / "session_savings.jsonl"


# session-file + managed-bash helpers moved to mcp.session_state (imported near the top).


def _workspace_session_state_file() -> Path:
    """Per-session runtime state (workflow runtime, ``session_phase``, credit
    hints), keyed by the resolved live session id.

    Previously a single ``workspaces/<hash>/session_state.json`` slot that every
    concurrent session in the workspace overwrote -- so two sessions running
    workflows in one repo corrupted each other's run state. Keying by the live
    session id isolates them. Falls back to the workspace bridge file before a
    session id is known (early startup / hostless callers).
    """
    sid = _resolve_live_session_id()
    if sid:
        from lemoncrow.core.foundation.paths import session_dir

        return session_dir(_lemoncrow_root(), _detect_agent(), sid) / "runtime_state.json"
    return _workspace_bridge_file()


# Window-anchored live-session resolution. Cached on this window's identity-file
# mtime so the common path (no SessionStart since last call) is a single stat;
# re-resolves only when SessionStart rewrote the file -- i.e. on startup /
# resume / clear / compact.
# moved to mcp.ledger (imported/re-exported near the top).
# This MCP process's (window_pid, window_btime), memoized: the claude-window
# ancestor is fixed for the server's whole life, so walk /proc only once.
# moved to mcp.ledger (imported/re-exported near the top).
# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


# moved to mcp.ledger (imported/re-exported near the top).


def _claude_session_id() -> str:
    """Live session UUID for *this* MCP server process's window.

    Resolved by :func:`_resolve_live_session_id`, which anchors to the window
    process so it stays correct across ``/clear`` and never adopts a sibling
    session's id from the shared workspace bridge. Empty for non-Claude hosts.
    """
    return _resolve_live_session_id()


def _read_workspace_session_state() -> dict[str, Any]:
    try:
        path = _workspace_session_state_file()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return {}


def _write_workspace_session_state(state: dict[str, Any]) -> None:
    path = _workspace_session_state_file()
    if path == _workspace_bridge_file():
        # No live session id yet (pre-SessionStart / hostless): do NOT write the
        # shared workspace bridge -- that single slot is SessionStart's to own.
        # Runtime state persists only once a per-session home exists.
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: str | None = None
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(state, handle, indent=2)
            tmp_path = handle.name
        Path(tmp_path).replace(path)
    except Exception:
        logging.exception("Recovered from broad exception handler")


# Process-local hint for the fast path in _process_tool_accounting: when False, a
# call that is neither a read nor a credit-enabled code-intel tool has no pending
# credits to age, so it can skip the session_state read+write entirely. Safe
# because one MCP server process owns the session's pending list; re-derived
# after every real pass.
_tool_accounting_pending_hint: bool = True


def _tool_accounting_hooks() -> ToolAccountingHooks:
    return ToolAccountingHooks(
        state_lock=_STATE_LOCK,
        read_workspace_state=_read_workspace_session_state,
        write_workspace_state=_write_workspace_session_state,
        session_id_getter=_claude_session_id,
        append_savings=_append_savings,
    )


def _process_tool_accounting(name: str, args: dict[str, Any], result: Any, rid: Any) -> None:
    _canonical_process_tool_accounting(
        name,
        args,
        result,
        rid,
        hooks=_tool_accounting_hooks(),
    )


def _default_workflow_agent_executor(
    step: Any,
    prompt: str,
    context_state: Any,
    *,
    route: Mapping[str, Any] | None = None,
) -> Any:
    import subprocess

    from lemoncrow.core.capabilities.workflow_spawn import build_spawn_envelope, compile_prompt_text
    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfigError
    from lemoncrow.pro.capabilities.cross_vendor_routing.router import NoFeasibleRouteError
    from lemoncrow.pro.capabilities.owned_execution_cache_affinity import (
        cache_affinity_hint,
        latest_cache_affinity,
    )

    workspace = _workspace_root().resolve()
    defaults = build_default_registry()
    decision: Any = None
    route_args = route if isinstance(route, Mapping) else {}
    route_mode = str(route_args.get("mode") or "native").strip() or "native"
    explicit_requested = any(str(route_args.get(field) or "").strip() for field in ("provider", "model", "runner"))
    explicit_requested = explicit_requested or route_mode == "explicit"
    cache_policy: OwnedCachePolicy = "fresh" if str(getattr(step, "context_mode", "") or "") == "fresh" else "inherit"
    compiled_prompt = compile_prompt_text(prompt)
    spawn_plan = context_state.spawn_plan_for_step(str(getattr(step, "step_id", "") or ""))
    spawn_envelope = build_spawn_envelope(
        step_id=str(getattr(step, "step_id", "") or ""),
        role_id=str(getattr(step, "role_id", "") or "general"),
        compiled_prompt=compiled_prompt,
        spawn_group_id=str(spawn_plan.get("spawn_group_id") or ""),
        cache_scope_id=str(spawn_plan.get("cache_scope_id") or ""),
        cache_policy=cache_policy,
    )
    affinity_state = (
        latest_cache_affinity(context_state.step_results, context_state.step_order) if cache_policy == "inherit" else {}
    )
    route_state = {
        "workflow_step": str(getattr(step, "step_id", "") or ""),
        "expected_input_tokens": max(1000, len(spawn_envelope.prompt) // 4),
        "session_phase": "execute",
        "spawn_group_id": spawn_envelope.spawn_group_id,
        "cache_scope_id": spawn_envelope.cache_scope_id,
        **cache_affinity_hint({"cache_affinity": affinity_state}),
    }
    if route_mode != "native":
        try:
            decision = _select_owned_execution_route(
                tool_name="agent",
                task_text=prompt,
                mode=route_mode,
                provider=str(route_args.get("provider") or ""),
                model=str(route_args.get("model") or ""),
                runner=str(route_args.get("runner") or ""),
                cache_policy=cache_policy,
                session_state=route_state,
            )
        except (RouteConfigError, NoFeasibleRouteError) as exc:
            if explicit_requested or route_mode == "auto":
                error = f"owned route selection failed: {exc}"
                return {
                    "status": "failed",
                    "output": "",
                    "output_json": {},
                    "execution_receipt": _native_workflow_execution_receipt(
                        defaults=defaults,
                        role_id=str(getattr(step, "role_id", "") or "general"),
                        compiled_prompt=compiled_prompt,
                        spawn_envelope=spawn_envelope.to_dict(),
                        status="failed",
                        error=error,
                        route_mode=route_mode,
                        attempted_route=True,
                    ),
                    "error": error,
                }
    if decision is not None:
        ledger = _get_ledger()
        try:
            execution = execute_owned_prompt(
                spawn_envelope.prompt,
                root=_lemoncrow_root(),
                tool_name="agent",
                task_text=spawn_envelope.prompt,
                decision=decision,
                host_agent=_detect_agent(),
                session_state=route_state,
                allow_fallback=decision.mode == "auto",
                cache_policy=cache_policy,
                compiled_prompt=compiled_prompt.to_dict(),
                spawn_metadata=spawn_envelope.to_dict(),
            )
        except OwnedExecutionError as exc:
            return {
                "status": "failed",
                "output": "",
                "output_json": {},
                "execution_receipt": exc.receipt.to_dict(),
                "duration_seconds": exc.receipt.duration_seconds,
                "cost_usd": exc.receipt.cost_usd,
                "error": str(exc),
            }
        ledger.record_call(
            operation="owned_execution",
            model=execution.receipt.executed_model,
            input_tokens=execution.receipt.input_tokens,
            output_tokens=execution.receipt.output_tokens,
            cache_read_tokens=execution.receipt.cache_read_input_tokens,
            cache_write_tokens=execution.receipt.cache_write_input_tokens,
            modeled_cache_read_tokens=execution.receipt.modeled_cache_read_input_tokens,
            cost_usd=execution.receipt.cost_usd,
            stable_prefix_hash=execution.receipt.stable_prefix_hash,
            prefix_invalidated_reason=execution.receipt.prefix_invalidated_reason,
            cache_evidence=execution.receipt.cache_evidence,
            phase="workflow",
        )
        return {
            "status": "done",
            "output": execution.output,
            "output_json": _parse_workflow_agent_output(execution.output),
            "execution_receipt": execution.receipt.to_dict(),
            "duration_seconds": execution.receipt.duration_seconds,
            "cost_usd": execution.receipt.cost_usd,
        }
    runner = decision.runner if decision is not None else _workflow_runner_profile()
    model = (
        decision.model
        if decision is not None
        else _workflow_runner_model(
            defaults,
            role_id=str(getattr(step, "role_id", "") or "general"),
            workspace=workspace,
            runner=runner,
        )
    )
    lane_key = ":".join(part for part in (spawn_envelope.spawn_group_id, spawn_envelope.role_id) if part)
    observed_lane = context_state.observed_host_lane(lane_key) if lane_key else {}
    selected_runner = str(observed_lane.get("runner") or runner)
    selected_model = str(observed_lane.get("model") or model or "")
    if lane_key and not observed_lane:
        context_state.record_host_lane(lane_key, {"runner": selected_runner, "model": selected_model})
    command = resolve_swarm_runner_command(
        runner=selected_runner,
        runner_model=selected_model,
        runner_args=(),
        child_command=(),
        prompt_template=spawn_envelope.prompt,
    )
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
            timeout=48 * 60 * 60,  # 48h hard ceiling so a hung host CLI can't wedge the run forever
        )
    except subprocess.TimeoutExpired:
        duration_seconds = time.perf_counter() - started
        error = f"native workflow spawn ({selected_runner}) timed out after 48h"
        return {
            "status": "failed",
            "output": "",
            "output_json": {},
            "execution_receipt": _native_workflow_execution_receipt(
                defaults=defaults,
                runner=selected_runner,
                model=selected_model,
                role_id=str(getattr(step, "role_id", "") or "general"),
                compiled_prompt=compiled_prompt,
                spawn_envelope=spawn_envelope.to_dict(),
                status="failed",
                duration_seconds=duration_seconds,
                observed_fields=_observed_host_fields(
                    spawn_envelope=spawn_envelope.to_dict(),
                    selected_runner=selected_runner,
                    selected_model=selected_model,
                ),
                unverified_fields=_unverified_host_fields(selected_model=selected_model),
                error=error,
                route_mode=route_mode,
            ),
            "error": error,
        }
    duration_seconds = time.perf_counter() - started
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        error = (completed.stderr or output or f"{selected_runner} exited with {completed.returncode}").strip()
        return {
            "status": "failed",
            "output": output,
            "output_json": {},
            "execution_receipt": _native_workflow_execution_receipt(
                defaults=defaults,
                runner=selected_runner,
                model=selected_model,
                role_id=str(getattr(step, "role_id", "") or "general"),
                compiled_prompt=compiled_prompt,
                spawn_envelope=spawn_envelope.to_dict(),
                status="failed",
                duration_seconds=duration_seconds,
                observed_fields=_observed_host_fields(
                    spawn_envelope=spawn_envelope.to_dict(),
                    selected_runner=selected_runner,
                    selected_model=selected_model,
                ),
                unverified_fields=_unverified_host_fields(selected_model=selected_model),
                error=error,
                route_mode=route_mode,
            ),
            "error": error,
        }
    return {
        "status": "done",
        "output": output,
        "output_json": _parse_workflow_agent_output(output),
        "execution_receipt": _native_workflow_execution_receipt(
            defaults=defaults,
            runner=selected_runner,
            model=selected_model,
            role_id=str(getattr(step, "role_id", "") or "general"),
            compiled_prompt=compiled_prompt,
            spawn_envelope=spawn_envelope.to_dict(),
            status="done",
            duration_seconds=duration_seconds,
            observed_fields=_observed_host_fields(
                spawn_envelope=spawn_envelope.to_dict(),
                selected_runner=selected_runner,
                selected_model=selected_model,
            ),
            unverified_fields=_unverified_host_fields(selected_model=selected_model),
            route_mode=route_mode,
        ),
    }


def _workflow_runner_profile() -> str:
    detected = _detect_agent()
    if detected in {"claude", "codex", "copilot", "opencode", "lemoncode"}:
        return detected
    return "claude"


def _workflow_runner_model(
    defaults: DefaultRegistry,
    *,
    role_id: str = "general",
    workspace: Path | None = None,
    runner: str | None = None,
) -> str | None:
    resolved_runner = runner or _workflow_runner_profile()
    workspace_model = resolve_host_model(
        resolved_runner,
        role_id,
        workspace_root=workspace,
        fallback=None,
    )
    if workspace_model:
        return normalize_model_for_host(resolved_runner, workspace_model)
    configured = str(_get_mcp_model() or os.environ.get("LEMONCROW_MODEL") or "").strip()
    if configured:
        return normalize_model_for_host(resolved_runner, configured)
    return None


def _native_workflow_execution_receipt(
    *,
    defaults: DefaultRegistry,
    status: str,
    runner: str | None = None,
    model: str | None = None,
    role_id: str = "",
    compiled_prompt: Any | None = None,
    spawn_envelope: dict[str, Any] | None = None,
    duration_seconds: float = 0.0,
    observed_fields: tuple[str, ...] = (),
    unverified_fields: tuple[str, ...] = (),
    error: str = "",
    route_mode: str = "native",
    attempted_route: bool = False,
) -> dict[str, Any]:
    resolved_runner = runner or _workflow_runner_profile()
    resolved_model = model or _workflow_runner_model(defaults) or ""
    resolved_provider = _provider_for_model(resolved_model) if resolved_model else ""
    expose_selection = attempted_route or route_mode == "native"
    compiled = compiled_prompt if hasattr(compiled_prompt, "stable_prefix_hash") else None
    envelope = dict(spawn_envelope or {})
    requested_fields = tuple(str(field) for field in envelope.get("requested_fields", ()))
    honored_fields = ("prompt",)
    dropped_fields = tuple(field for field in requested_fields if field not in honored_fields)
    return {
        "status": status,
        "mode": route_mode,
        "role_id": role_id,
        "selected_provider": resolved_provider if expose_selection else "",
        "selected_model": resolved_model if expose_selection else "",
        "selected_runner": resolved_runner if expose_selection else "",
        "selected_transport": "host-cli" if expose_selection else "",
        "executed_provider": "",
        "executed_model": "",
        "executed_runner": resolved_runner if status == "done" else "",
        "executed_transport": "host-cli" if status == "done" else "",
        "request_id": "",
        "duration_seconds": duration_seconds,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "modeled_cache_read_input_tokens": 0,
        "stable_prefix_hash": getattr(compiled, "stable_prefix_hash", ""),
        "stable_prefix_tokens": getattr(compiled, "stable_prefix_tokens", 0),
        "dynamic_tokens": getattr(compiled, "dynamic_tokens", 0),
        "prefix_invalidated_reason": "cache_policy_fresh" if str(envelope.get("cache_policy") or "") == "fresh" else "",
        "cache_evidence": "hint_only" if getattr(compiled, "stable_prefix_hash", "") else "none",
        "cache_capability": "hint_only" if getattr(compiled, "stable_prefix_hash", "") else "none",
        "spawn_group_id": str(envelope.get("spawn_group_id") or ""),
        "cache_scope_id": str(envelope.get("cache_scope_id") or ""),
        "cache_policy": str(envelope.get("cache_policy") or "inherit"),
        "eligible_for_reuse": bool(
            getattr(compiled, "stable_prefix_hash", "") and str(envelope.get("cache_policy") or "inherit") != "fresh"
        ),
        "reuse_observed": False,
        "spawn_latency_ms": int(duration_seconds * 1000),
        "requested_fields": list(requested_fields),
        "honored_fields": list(observed_fields or honored_fields),
        "dropped_fields": list(dropped_fields),
        "observed_fields": list(observed_fields),
        "unverified_fields": list(unverified_fields),
        "observation_mode": "runtime-observed",
        "cost_usd": 0.0,
        "rerouted": False,
        "attempts": [],
        "error": error,
    }


def _observed_host_fields(
    *,
    spawn_envelope: dict[str, Any],
    selected_runner: str,
    selected_model: str,
) -> tuple[str, ...]:
    # Only fields the host CLI actually receives count as observed/honored: the
    # prompt always crosses the process boundary and the model only when a
    # --model flag is emitted. cache_policy / spawn_group_id / cache_scope_id /
    # role_id are never passed to the subprocess (resolve_swarm_runner_command),
    # so listing them as honored would overstate what the host actually did.
    observed = ["prompt"]
    if selected_runner:
        observed.append("selected_runner")
    if selected_model:
        observed.append("selected_model")
    return tuple(observed)


def _unverified_host_fields(*, selected_model: str) -> tuple[str, ...]:
    fields = ["executed_provider", "executed_transport", "reuse_observed"]
    if selected_model:
        fields.append("executed_model")
    return tuple(fields)


def _parse_workflow_agent_output(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _workflow_spawn_summary(step_results: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "step_count": 0,
        "eligible_for_reuse": 0,
        "reuse_observed": 0,
        "spawn_latency_ms": 0,
        "cache_capability_counts": {},
        "host_dropped_fields": {},
    }
    for step_result in step_results.values():
        receipt = getattr(step_result, "execution_receipt", None)
        if not isinstance(receipt, Mapping):
            continue
        if not any(
            key in receipt
            for key in (
                "cache_capability",
                "spawn_group_id",
                "cache_scope_id",
                "requested_fields",
                "dropped_fields",
            )
        ):
            continue
        summary["step_count"] += 1
        summary["eligible_for_reuse"] += int(bool(receipt.get("eligible_for_reuse", False)))
        summary["reuse_observed"] += int(bool(receipt.get("reuse_observed", False)))
        summary["spawn_latency_ms"] += int(receipt.get("spawn_latency_ms", 0) or 0)
        capability = str(receipt.get("cache_capability") or "").strip()
        if capability:
            counts = cast(dict[str, int], summary["cache_capability_counts"])
            counts[capability] = int(counts.get(capability, 0) or 0) + 1
        dropped = receipt.get("dropped_fields")
        if isinstance(dropped, list | tuple):
            for field in dropped:
                field_name = str(field).strip()
                if not field_name:
                    continue
                drop_counts = cast(dict[str, int], summary["host_dropped_fields"])
                drop_counts[field_name] = int(drop_counts.get(field_name, 0) or 0) + 1
    return summary if summary["step_count"] else {}


def _select_owned_execution_route(
    *,
    tool_name: str,
    task_text: str,
    mode: str,
    provider: str,
    model: str,
    runner: str,
    cache_policy: OwnedCachePolicy = "inherit",
    session_state: Mapping[str, Any] | None = None,
) -> Any:
    return select_owned_route(
        _lemoncrow_root(),
        OwnedRouteRequest(
            tool_name=tool_name,
            task_text=task_text,
            mode="explicit" if mode == "explicit" else "auto",
            provider=provider.strip().lower(),
            model=model.strip(),
            runner=runner.strip().lower(),
            host_agent=_detect_agent(),
            cache_policy="fresh" if cache_policy == "fresh" else "inherit",
            session_state=dict(session_state or {}),
        ),
    )


_WORKFLOW_SPAWN_DEPTH_LIMIT = 8
# Per-worker-thread workflow spawn depth. The dispatcher runs a thread pool, so
# tracking depth in os.environ races across parallel workflow steps and can leak
# a stale value into the process env; a threading.local isolates it per thread.
_workflow_spawn_depth: threading.local = threading.local()
# Tools that themselves spawn sub-agents/workflows: invoking them from a
# workflow step opens an unbounded recursive spawn path, so they are blocked.
_WORKFLOW_SPAWNING_TOOLS = frozenset({"workflow"})


def _default_workflow_tool_executor(step: Any, args: dict[str, Any], context_state: Any) -> Any:
    if step.tool in _WORKFLOW_SPAWNING_TOOLS:
        raise ValueError(f"workflow steps cannot invoke spawning tool {step.tool!r} (unbounded recursion)")
    spec = TOOLS.get(step.tool)
    if spec is None:
        raise ValueError(f"unknown workflow tool: {step.tool}")
    depth = getattr(_workflow_spawn_depth, "value", 0)
    if depth >= _WORKFLOW_SPAWN_DEPTH_LIMIT:
        raise ValueError(
            f"workflow spawn depth limit ({_WORKFLOW_SPAWN_DEPTH_LIMIT}) exceeded; aborting recursive tool execution"
        )
    handler = cast(Callable[[dict[str, Any]], Any], spec["handler"])
    _workflow_spawn_depth.value = depth + 1
    try:
        return handler(args)
    finally:
        _workflow_spawn_depth.value = depth


def _default_workflow_shell_executor(step: Any, command: str, forked_context: dict[str, Any]) -> Any:
    if getattr(step, "fork_from", None):
        # The shell tool handler has no parameter to receive forked context, so a
        # fork_from on a shell step would be silently dropped. Reject it loudly
        # rather than give the author a false sense that the fork took effect.
        raise ValueError("workflow shell steps do not support fork_from (the shell tool cannot receive forked context)")
    spec = TOOLS.get("bash")
    if spec is None:
        raise ValueError("bash tool not registered")
    handler = cast(Callable[[dict[str, Any]], Any], spec["handler"])
    return handler({"command": command})


def _run_owned_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    from lemoncrow.core.capabilities.workflow_context import WorkflowContextState
    from lemoncrow.core.capabilities.workflow_runner import WorkflowRunner
    from lemoncrow.core.capabilities.workflow_schema import workflow_definition_from_mapping

    resume = bool(arguments.get("resume", False))
    # Hold _STATE_LOCK across the whole read-modify-write: atomic os.replace
    # prevents a torn file but not a lost update when a concurrent handler
    # interleaves its own session_state RMW. _STATE_LOCK is an RLock, so the
    # reentrant helper calls below are safe.
    with _STATE_LOCK:
        session_state = _read_workspace_session_state()
        runtime_state = _workflow_runtime_state(session_state)

        workflow_raw = arguments.get("workflow")
        if resume and not isinstance(workflow_raw, Mapping):
            workflow_raw = runtime_state.get("workflow")
        if not isinstance(workflow_raw, Mapping):
            raise ValueError("workflow run requires workflow mapping")
        route_raw = arguments.get("route")
        if resume and not isinstance(route_raw, Mapping):
            route_raw = runtime_state.get("route")
        route = dict(route_raw) if isinstance(route_raw, Mapping) else {}
        review_raw = arguments.get("plan_review")
        plan_review = dict(review_raw) if isinstance(review_raw, Mapping) else {}
        review_decision = _coerce_workflow_review_decision(plan_review)
        definition = workflow_definition_from_mapping(workflow_raw)
        workflow_state = (
            dict(session_state.get("workflow") or {}) if isinstance(session_state.get("workflow"), dict) else {}
        )
        runner_state = (
            WorkflowContextState.from_mapping(runtime_state.get("runner")) if resume else WorkflowContextState()
        )
        runner = WorkflowRunner(
            agent_executor=lambda step, prompt, context_state: _default_workflow_agent_executor(
                step,
                prompt,
                context_state,
                route=route,
            ),
            tool_executor=_default_workflow_tool_executor,
            shell_executor=_default_workflow_shell_executor,
        )
        ledger = _get_ledger()
        result = runner.run(
            definition,
            context_state=runner_state,
            ledger=ledger,
            plan_review_decision=review_decision,
        )
        spawn_summary = _workflow_spawn_summary(result.step_results)
        created_at = str(runtime_state.get("created_at") or "").strip() if resume else ""
        runtime_state = {
            "run_id": result.run_id,
            "workflow_id": definition.workflow_id,
            "workflow": dict(workflow_raw),
            "route": dict(route),
            "status": result.status,
            "step_order": list(result.step_order),
            "current_step": result.paused_step_id
            or result.failed_step_id
            or (result.step_order[-1] if result.step_order else ""),
            "failed_step_id": result.failed_step_id or "",
            "paused_step_id": result.paused_step_id or "",
            "artifact_ids": [],
            "created_at": created_at or datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "runner": runner_state.to_dict(),
        }
        if spawn_summary:
            runtime_state["spawn_summary"] = dict(spawn_summary)
        if result.status == "awaiting_review":
            workflow_state["current_step"] = "review"
            workflow_state["session_phase"] = "review"
            runtime_state["plan_review"] = {
                "decision": review_decision or "pending",
                "paused_step_id": result.paused_step_id or "",
                "workflow_id": definition.workflow_id,
            }
            ledger.record_workflow_event(
                "plan_review",
                {
                    "workflow_step": "review",
                    "review_decision": "pending",
                    "workflow_id": definition.workflow_id,
                    "step_id": result.paused_step_id or "",
                },
            )
        elif result.status == "review_rejected":
            workflow_state["current_step"] = "review"
            workflow_state["session_phase"] = "review"
            runtime_state["plan_review"] = {
                "decision": review_decision or "revise",
                "paused_step_id": result.paused_step_id or "",
                "workflow_id": definition.workflow_id,
            }
            ledger.record_workflow_event(
                "plan_review",
                {
                    "workflow_step": "review",
                    "review_decision": review_decision or "revise",
                    "workflow_id": definition.workflow_id,
                    "step_id": result.paused_step_id or "",
                },
            )
        else:
            workflow_state["current_step"] = "execution"
            workflow_state["session_phase"] = "execute"
            if review_decision:
                runtime_state["plan_review"] = {
                    "decision": review_decision,
                    "workflow_id": definition.workflow_id,
                }
            if review_decision:
                ledger.record_workflow_event(
                    "plan_review",
                    {
                        "workflow_step": "review",
                        "review_decision": review_decision,
                        "workflow_id": definition.workflow_id,
                    },
                )
        workflow_state["current_task"] = {
            "workflow_id": definition.workflow_id,
            "run_id": result.run_id,
            "step_id": result.paused_step_id
            or result.failed_step_id
            or (result.step_order[-1] if result.step_order else ""),
        }
        workflow_state["task_outputs"] = {
            step_id: step_result.to_dict() for step_id, step_result in result.step_results.items()
        }
        if spawn_summary:
            workflow_state["spawn_summary"] = dict(spawn_summary)
            ledger.record_workflow_event("spawn_summary", dict(spawn_summary))
        if result.status in {"awaiting_review", "review_rejected"}:
            workflow_state["plan_review"] = {
                "decision": review_decision or "pending",
                "paused_step_id": result.paused_step_id or "",
                "workflow_id": definition.workflow_id,
            }
        elif review_decision:
            workflow_state["plan_review"] = {
                "decision": review_decision,
                "workflow_id": definition.workflow_id,
            }
        else:
            workflow_state.pop("plan_review", None)
        workflow_state["updated_at"] = datetime.now(UTC).isoformat()
        session_state["workflow"] = workflow_state
        _write_workflow_runtime_state(session_state, runtime_state)
        _write_workspace_session_state(session_state)
        ledger.persist()
    receipt = {
        "run_id": result.run_id,
        "status": result.status,
        "step_count": len(result.step_order),
        "artifact_ids": [],
    }
    if spawn_summary:
        receipt["spawn_summary"] = dict(spawn_summary)
    if result.failed_step_id:
        receipt["failed_step_id"] = result.failed_step_id
    if result.paused_step_id:
        receipt["paused_step_id"] = result.paused_step_id
    return receipt


def _orchestration_handler_hooks() -> OrchestrationHandlerHooks:
    return OrchestrationHandlerHooks(
        workspace_root=_workspace_root,
        read_workspace_session_state=_read_workspace_session_state,
        detect_agent=_detect_agent,
        select_owned_route=select_owned_route,
        execute_owned_prompt=execute_owned_prompt,
        state_lock=_STATE_LOCK,
        run_owned_workflow=_run_owned_workflow,
        coerce_workflow_runtime_status=_coerce_workflow_runtime_status,
        inspect_workflow_runtime=_inspect_workflow_runtime,
        require_active_workflow_runtime=_require_active_workflow_runtime,
        pause_workflow_runtime=_pause_workflow_runtime,
        write_workspace_session_state=_write_workspace_session_state,
        stop_workflow_runtime=_stop_workflow_runtime,
    )


configure_orchestration_handler_hooks(_orchestration_handler_hooks)


def _inspect_workflow_runtime(session_state: dict[str, Any]) -> dict[str, Any]:
    from lemoncrow.core.capabilities.workflow_context import WorkflowContextState

    status = _coerce_workflow_runtime_status(session_state)
    runtime_state = _workflow_runtime_state(session_state)
    runner_state = WorkflowContextState.from_mapping(runtime_state.get("runner"))
    step_spawns: list[dict[str, Any]] = []
    for step_id in runner_state.step_order:
        step_result = runner_state.step_results.get(step_id)
        if step_result is None:
            continue
        receipt = step_result.execution_receipt
        if not isinstance(receipt, Mapping):
            continue
        if not any(
            key in receipt
            for key in (
                "cache_capability",
                "spawn_group_id",
                "cache_scope_id",
                "requested_fields",
                "dropped_fields",
            )
        ):
            continue
        step_spawns.append(
            {
                "step_id": step_id,
                "status": step_result.status,
                "mode": str(receipt.get("mode") or ""),
                "role_id": str(receipt.get("role_id") or ""),
                "cache_capability": str(receipt.get("cache_capability") or ""),
                "eligible_for_reuse": bool(receipt.get("eligible_for_reuse", False)),
                "reuse_observed": bool(receipt.get("reuse_observed", False)),
                "spawn_latency_ms": int(receipt.get("spawn_latency_ms", 0) or 0),
                "spawn_group_id": str(receipt.get("spawn_group_id") or ""),
                "cache_scope_id": str(receipt.get("cache_scope_id") or ""),
                "requested_fields": list(receipt.get("requested_fields") or []),
                "honored_fields": list(receipt.get("honored_fields") or []),
                "dropped_fields": list(receipt.get("dropped_fields") or []),
            }
        )
    spawn_summary = runtime_state.get("spawn_summary") if isinstance(runtime_state.get("spawn_summary"), dict) else {}
    return {
        **status,
        "spawn_summary": spawn_summary,
        "step_spawns": step_spawns,
    }


def _is_registrable_workspace(ws: Path) -> bool:
    """Reject ``$HOME`` and ``/`` as workspace roots.

    Registering either lets the code-warm daemon (``code_warm.py``) try to
    reindex that entire tree on every ~15s poll, forever: such a root never
    finishes indexing (or writes a ``session_state.json``), so it also never
    becomes eligible for ``lc code prune`` to reclaim -- it just burns a
    CPU core indefinitely for as long as the registering process stays
    alive. Concretely: a host launched with cwd/``CLAUDE_WORKSPACE_ROOT``
    pointed at ``$HOME`` (or ``/``) rather than a repo.
    """
    return ws != Path.home() and ws != Path(ws.anchor)


def _register_mcp_session() -> None:
    """Create this MCP process's registration file if it doesn't exist yet."""
    f = _mcp_session_file()
    if f.exists():
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        from lemoncrow.core.foundation.paths import is_recognized_workspace as _is_recognized_workspace

        configured = os.environ.get("CLAUDE_WORKSPACE_ROOT", "").strip()
        if configured:
            # An explicit host env var is authoritative (matches
            # resolve_workspace_root's own precedence) -- still subject to
            # the $HOME/`/` sanity check below, but not the git-repo check.
            ws_path = Path(configured).resolve()
        else:
            cwd = Path(os.getcwd()).resolve()
            if not _is_recognized_workspace(cwd):
                _log.warning(
                    "Refusing to register MCP session workspace %s: not a git "
                    "repository and not registered via `lc init` -- would "
                    "make the code-warm daemon index an arbitrary directory "
                    "forever. Run `lc init` here, or launch from inside a "
                    "git repository.",
                    cwd,
                )
                return
            ws_path = cwd
        if not _is_registrable_workspace(ws_path):
            _log.warning(
                "Refusing to register MCP session workspace %s: it's $HOME "
                "or / itself, not a single project -- would make the "
                "code-warm daemon reindex it forever. Set "
                "CLAUDE_WORKSPACE_ROOT to the specific repo.",
                ws_path,
            )
            return
        ws = str(ws_path)
        from lemoncrow.core.foundation.paths import workspace_key as _workspace_key
        from lemoncrow.core.service.project_registry import register_project

        registered = register_project(ws_path)
        # A workspace may intentionally aggregate several repos. Register those
        # roots too so this one long-lived MCP process can safely route later
        # requests to them by project id (or an exact legacy registered path).
        with contextlib.suppress(Exception):
            from lemoncrow.pro.capabilities.code_context.workspace_config import load_workspace_config

            workspace_config = load_workspace_config(ws_path)
            if workspace_config is not None:
                for repo in workspace_config.repos:
                    register_project(repo.repo_root)

        data = {
            "lemoncrow_mcp_id": _MCP_ID,
            "pid": os.getpid(),
            "workspace": ws,
            "workspace_hash": _workspace_key(ws),
            "project_id": registered.project_id,
            "started_at": datetime.utcnow().isoformat(),
            "claude_session_id": "",
            "model": "",
            "managed_bash": [],
        }
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        # Best-effort sidecar registration; a failed write must not break startup.
        _log.debug("MCP session registration write failed", exc_info=True)


def _unregister_mcp_session() -> None:
    """Remove this MCP process's registration file on clean shutdown."""
    try:
        _mcp_session_file().unlink(missing_ok=True)
    except OSError:
        _log.debug("MCP session registration cleanup failed", exc_info=True)


# moved to mcp.ledger (imported/re-exported near the top).


# Host/session identity resolution lives in mcp.session_identity.


def _get_host_session_sidecar_path() -> Path:
    """Return the per-session savings sidecar path for the current host.

    The sidecar MUST land under the *currently active* session, because the
    readers (Stop hook, statusline, session report) are always handed the
    active session id by the host and look there.  The MCP server process is
    long-lived and survives many /clear cycles, so its launch-time
    ``CLAUDE_CODE_SESSION_ID`` goes stale the moment the user first runs /clear
    and stays stale for the rest of the process's life.

    Resolution is delegated to :func:`_resolved_host_session_id` (window-anchored
    Claude id, then native host env ids). When nothing resolves it falls back to
    the workspace-shared path -- but callers that must not cross-attribute gate
    on :func:`_resolved_host_session_id` first: :func:`_append_savings` diverts
    to the per-workspace quarantine ledger and :func:`_write_statusline_sidecar`
    skips, so neither ever writes that shared slot.
    """
    sid, host = _resolved_host_session()
    if sid:
        cache_key = f"{host}:{sid}"
        if cache_key not in _SAVINGS_SIDECAR_PATH_BY_SID:
            from lemoncrow.core.foundation.paths import session_dir

            _SAVINGS_SIDECAR_PATH_BY_SID[cache_key] = (
                session_dir(_lemoncrow_root(), host or _detect_agent(), sid) / "savings.jsonl"
            )
        return _SAVINGS_SIDECAR_PATH_BY_SID[cache_key]
    return _workspace_savings_path()


def _current_context_state() -> tuple[int, str]:
    """Measured (context size, model) from the host transcript's last usage entry.

    Context size is input + cache_read + cache_creation tokens of the most
    recent usage entry; model is the one that produced it — the per-turn ground
    truth, unlike the SessionStart bridge which goes stale when the user
    switches models mid-session via /model. Returns (0, "") when no
    transcript/usage is available. Callers must treat 0/"" as "unknown" and
    skip pricing — never synthesize values.

    Cached on the candidate transcripts' (path, mtime_ns, size) signature: this
    runs on every savings-bearing tool call, so we re-tail and JSON-parse the
    64 KB window only when a transcript actually changed.
    """
    try:
        from lemoncrow.core.capabilities.savings_summary import (
            claude_transcript_candidates,
            is_real_model,
        )

        sid = _claude_session_id()
        if sid:
            candidates = list(claude_transcript_candidates(sid))
            sig_parts: list[tuple[str, int, int]] = []
            for cand in candidates:
                try:
                    st = os.stat(cand)
                except OSError:
                    continue
                sig_parts.append((str(cand), st.st_mtime_ns, st.st_size))
            sig = tuple(sig_parts)

            with _STATE_LOCK:
                cached = _CONTEXT_STATE_CACHE.get(sid)
            if cached is not None and cached[0] == sig:
                return cached[1]

            from lemoncrow.gateway.hosts.context_state import _tail_lines

            result: tuple[int, str] = (0, "")
            for cand in candidates:
                try:
                    tail_lines = _tail_lines(cand)
                except OSError:
                    continue
                best = 0
                best_model = ""
                for line in tail_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # first line of the tail window may be partial
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    ctx = (
                        int(usage.get("input_tokens", 0) or 0)
                        + int(usage.get("cache_read_input_tokens", 0) or 0)
                        + int(usage.get("cache_creation_input_tokens", 0) or 0)
                    )
                    if ctx > 0:
                        best = ctx
                        candidate = str(msg.get("model") or "").strip()
                        if is_real_model(candidate):
                            best_model = candidate
                if best > 0:
                    result = (best, best_model)
                    break

            with _STATE_LOCK:
                _CONTEXT_STATE_CACHE[sid] = (sig, result)
            return result

        # --- Non-Claude hosts (OpenCode, Codex): read from workspace bridge ---
        bridge_sid, host = _resolved_host_session()
        if bridge_sid and host and host != "claude":
            cache_key = f"bridge:{host}:{bridge_sid}"
            from lemoncrow.core.foundation.paths import session_dir

            # Freshness signature over the bridge stats file (mtime+size) so a
            # cached entry is recomputed when the host plugin updates it.
            stats_path = session_dir(_lemoncrow_root(), host, bridge_sid) / "stats.json"
            try:
                bst = os.stat(stats_path)
                sig = ((str(stats_path), bst.st_mtime_ns, bst.st_size),)
            except OSError:
                sig = ()
            with _STATE_LOCK:
                cached = _CONTEXT_STATE_CACHE.get(cache_key)
            if cached is not None and cached[0] == sig:
                return cached[1]
            result = _bridge_context_state(bridge_sid, host)
            if result[0] > 0:
                with _STATE_LOCK:
                    _CONTEXT_STATE_CACHE[cache_key] = (sig, result)
            return result

    except Exception:
        logging.exception("Recovered from broad exception handler")
        _log.debug("context state probe failed", exc_info=True)
    return 0, ""


def _bridge_context_state(session_id: str, host: str) -> tuple[int, str]:
    """Read context tokens from the workspace bridge or host-specific data.

    For non-Claude hosts (OpenCode, Codex) that don't produce Claude-format
    transcript JSONL, this reads the session stats file maintained by the host
    plugin.
    """
    try:
        from lemoncrow.core.foundation.paths import session_dir

        root = _lemoncrow_root()
        stats_path = session_dir(root, host, session_id) / "stats.json"
        if not stats_path.is_file():
            return 0, ""
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0, ""
        if not isinstance(stats, dict):
            return 0, ""
        # Prefer the bounded current-turn context size (this turn's real,
        # context-window-capped request size -- Codex populates this from its
        # transcript's last_token_usage; see _codex_transcript_snapshot).
        # Falling back to state["usage"] is a LAST RESORT: that field is a
        # cumulative running total across the whole session and grows
        # unboundedly turn over turn, which would price every avoided call as
        # if it re-sent the entire session's tokens instead of one request.
        ctx = int(stats.get("current_turn_context_tokens", 0) or 0)
        if ctx <= 0:
            _usage = stats.get("usage")
            usage = _usage if isinstance(_usage, dict) else {}
            ctx = (
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("cache_read_tokens", 0) or 0)
                + int(usage.get("cache_write_tokens", 0) or 0)
            )
        model = str(stats.get("last_model") or stats.get("model") or "").strip()
        return ctx, model
    except Exception:
        _log.debug("bridge context state probe failed for %s/%s", host, session_id, exc_info=True)
    return 0, ""


def _price_avoided_calls_usd(
    model: str,
    calls_saved: int,
    ctx_tokens: int,
    *,
    avg_output_tokens: int = 0,
    long_context: bool = False,
) -> float:
    """Price avoided tool-call round trips: context re-read + the turn's output.

    Each avoided call is an API round trip that would have (a) re-read the
    current context (ctx_tokens, measured from the host transcript) at the
    cache-read rate, and (b) produced the session's average per-turn output
    (avg_output_tokens, measured) — billed at the output rate and
    re-entering context as a cache write on the next turn. Per-request flat
    semantics, at the >200k premium rate when the live window itself ran
    long-context. Unknown model or unmeasured context → 0.0 (no guess).
    """
    if calls_saved <= 0 or ctx_tokens <= 0 or not model or model == "_default":
        return 0.0
    from lemoncrow.core.capabilities.pricing import get_model_pricing

    pricing = get_model_pricing(model)
    if pricing is None or not pricing.known or pricing.cache_read <= 0:
        return 0.0
    out = max(0, int(avg_output_tokens))
    return pricing.request_cost_usd(
        cache_read_tokens=int(calls_saved) * int(ctx_tokens),
        output_tokens=int(calls_saved) * out,
        cache_write_tokens=int(calls_saved) * out,
        long_context=long_context,
    )


def _session_avg_output_tokens() -> int:
    """Measured average output tokens per assistant turn for the live session.

    0 = unknown (no transcript yet); callers price the output component of an
    avoided roundtrip only when this is measured — never a synthesized value.
    """
    try:
        from lemoncrow.core.capabilities.savings_summary import (
            claude_transcript_candidates,
            read_transcript_stats,
        )

        sid = _claude_session_id()
        if not sid:
            return 0
        paths = list(claude_transcript_candidates(sid))
        if not paths:
            return 0
        stats = read_transcript_stats(paths[0])
        if stats is None:
            return 0
        # output_tokens (thinking + text + tool_use JSON — everything billed at
        # the output rate) includes subagent transcripts, so divide by ALL
        # assistant turns (main + subagent) or the average inflates whenever
        # subagents ran.
        turns = stats.turns + sum(len(sub) for sub in stats.subagent_turn_timestamps)
        if turns <= 0:
            return 0
        return max(0, int(stats.output_tokens / turns))
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return 0


def _savings_long_context(model: str, ctx_tokens: int) -> bool:
    """True when the live window is past *model*'s >200k premium threshold.

    Savings earned there are worth the same premium rates the cost side bills
    for those requests (long-context parity). Models without a premium tier
    on their rate card (threshold 0) always return False.
    """
    if ctx_tokens <= 0 or not model or model == "_default":
        return False
    try:
        from lemoncrow.core.capabilities.pricing import get_model_pricing

        pricing = get_model_pricing(model)
    except Exception:
        return False
    if pricing is None or not pricing.known:
        return False
    threshold = pricing.long_context_threshold()
    return threshold > 0 and ctx_tokens > threshold


def _append_savings(tool_name: str, tokens_saved: int, calls_saved: int, rid: str = "") -> None:
    """Append one per-call savings row to ``sessions/<id>/savings.jsonl``.

    This single sidecar is the source of truth read by the live statusline,
    the stop hook, and the session report. Each row carries the raw
    ``tokens``/``calls`` plus the pre-priced ``cost_saved_usd`` /
    ``calls_usd`` so analytics readers need not re-price.
    """
    # A latency-profile run (`lc perf`) drives `_handle` with synthetic
    # probe calls; those aren't work the agent avoided, so don't credit them.
    if os.environ.get("LEMONCROW_TOOL_PROFILE_PATH"):
        return
    if tokens_saved <= 0 and calls_saved <= 0:
        return
    # Fail closed for ATTRIBUTION, not for ACCOUNTING: with no resolvable
    # session id the sidecar path would fall back to a workspace-shared file
    # that every concurrent window in this repo reads, cross-attributing one
    # window's savings to another. But dropping the row silently understates
    # every historical window (whole resumed sessions have lost 100+ credited
    # calls this way). Route it to a per-workspace quarantine ledger under
    # sessions/ instead: the day-bucketed aggregate counts any savings.jsonl
    # there (see savings_summary._scan_savings_files), while per-session
    # readers glob by exact session id and never see it.
    unattributed = not _resolved_host_session_id()
    if unattributed:
        _log.debug("savings append unattributed: unresolved session id (quarantine ledger)")
    else:
        _register_mcp_session()
    ts = datetime.utcnow().isoformat()
    # Per-turn model truth from the transcript beats the SessionStart bridge,
    # which goes stale when the user switches models mid-session via /model.
    ctx_tokens, live_model = _current_context_state()
    model = live_model or _get_mcp_model()
    # Long-context parity: past the model's >200k premium threshold every
    # request bills at premium rates, so tokens/calls saved there are worth
    # the premium too. Stamped on the row so carry pricing
    # (savings_summary._carry_credit) applies the same rate later.
    long_ctx = _savings_long_context(model, ctx_tokens)
    calls_usd = 0.0
    if calls_saved > 0 and ctx_tokens > 0:
        calls_usd = round(
            _price_avoided_calls_usd(
                model,
                calls_saved,
                ctx_tokens,
                avg_output_tokens=_session_avg_output_tokens(),
                long_context=long_ctx,
            ),
            6,
        )
    cost_saved = round(_price_tokens_saved_usd(model, tokens_saved, long_context=long_ctx), 6)
    try:
        if unattributed:
            from lemoncrow.core.foundation.paths import session_dir

            path = (
                session_dir(_lemoncrow_root(), _detect_agent(), f"unattributed-{_workspace_ws_hash()}")
                / "savings.jsonl"
            )
        else:
            path = _get_host_session_sidecar_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "tool": tool_name,
            # Field names match the in-response `saved: {tokens, calls}` shape.
            # The file lives under sessions/<id>/ so "savings" is implicit
            # from context — no need to suffix the keys.
            "tokens": int(tokens_saved),
            "calls": int(calls_saved),
            "model": model,
            "ts": ts,
        }
        if long_ctx:
            entry["long_context"] = True
        if unattributed:
            # Diagnosable marker: the row counts in windowed totals but belongs
            # to no session; readers that key by session ignore this file.
            entry["unattributed"] = True
        # Pre-priced USD so the session report / analytics need not re-price.
        # Omitted when zero to keep rows lean; readers treat missing as 0.
        if cost_saved > 0:
            entry["cost_saved_usd"] = cost_saved
        if calls_usd > 0:
            entry["calls_usd"] = calls_usd
            entry["ctx_tokens"] = ctx_tokens
        if rid:
            entry["rid"] = rid
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        # Fold the new row into the in-memory historical window totals (O(1)).
        # Invalidating instead would force a full sessions/** re-scan on the
        # next statusline render for every append; the cache's normal TTL
        # still refreshes the totals to pick up other processes' writes.
        try:
            from lemoncrow.core.capabilities.savings_summary import _bump_historical_savings_cache as _bump_cache

            _bump_cache(entry)
        except ImportError:
            pass
    except Exception:
        logging.exception("Recovered from broad exception handler")
        # Best-effort savings sidecar; a failed write must not break the tool call.
        _log.debug("savings sidecar append failed", exc_info=True)
        return
    # Refresh the statusline sidecar so statusline.sh can read it without a
    # subprocess.  Rate-limited internally; never raises.
    _write_statusline_sidecar()


def _append_workspace_savings(tool_name: str, tokens_saved: int, calls_saved: int, rid: str = "") -> None:
    """Backward-compat shim — delegates to _append_savings."""
    _append_savings(tool_name, tokens_saved, calls_saved, rid=rid)


_STATUSLINE_SIDECAR_MIN_INTERVAL: float = 5.0  # seconds — rate-limit transcript reads
_statusline_wake = threading.Event()
_statusline_worker_lock = threading.Lock()
_statusline_worker_started = False
# (session_id, host) pairs awaiting a segment write. Captured on the DISPATCH
# thread — under the singleton daemon the request session context is a
# thread-local the worker thread cannot see, and one daemon serves many
# windows, so the worker must be told which sessions to render for.
_statusline_pending: set[tuple[str, str]] = set()
_statusline_pending_lock = threading.Lock()


def _statusline_sidecar_loop() -> None:
    """Daemon worker: compute + write the statusline sidecar off the tool-call thread.

    savings_frames on a long session costs real CPU (sidecar decode + carry
    credit); computed inline in the tool handler it landed as multi-second
    stalls on whatever query ran concurrently (profiled at 3-4s on a large
    dev session before the _window_carry rewrite). Wake-ups coalesce: at most
    one compute per _STATUSLINE_SIDECAR_MIN_INTERVAL, latest wins.
    """
    while True:
        _statusline_wake.wait()
        _statusline_wake.clear()
        with _statusline_pending_lock:
            pending = sorted(_statusline_pending)
            _statusline_pending.clear()
        for sid, host in pending:
            try:
                _write_statusline_sidecar_now(sid, host)
            except Exception:
                _log.debug("statusline sidecar write failed", exc_info=True)
        # Rate-limit AFTER the write: the first event renders immediately and a
        # burst coalesces into one trailing refresh (wakes set during compute or
        # sleep re-enter the loop, so the sidecar never goes stale after a burst).
        time.sleep(_STATUSLINE_SIDECAR_MIN_INTERVAL)


def _write_statusline_sidecar() -> None:
    """Signal the statusline worker; never compute on the caller's thread."""
    global _statusline_worker_started
    # Resolve the session HERE, on the dispatch thread that still carries the
    # request context; the worker thread would resolve the daemon's own window.
    sid, host = _resolved_host_session()
    if not sid:
        return
    with _statusline_pending_lock:
        _statusline_pending.add((sid, host or _detect_agent()))
    if not _statusline_worker_started:
        with _statusline_worker_lock:
            if not _statusline_worker_started:
                threading.Thread(
                    target=_statusline_sidecar_loop, name="lemoncrow-statusline-sidecar", daemon=True
                ).start()
                _statusline_worker_started = True
    _statusline_wake.set()


def _write_statusline_sidecar_now(session_id: str = "", host: str = "") -> None:
    """Write the current savings segment to sessions/<resolved_id>/statusline_segment.

    Runs on the statusline worker thread after every burst of savings events so
    statusline.sh can read the pre-computed segment directly without spawning a
    subprocess.  Uses the same resolved session id as the savings sidecar, so
    the statusline and MCP server are always in sync regardless of /clear or
    --resume.
    """
    # Fail closed: an unresolved session id would otherwise resolve to the
    # workspace-shared slot and publish one window's segment for every sibling.
    sid = session_id or _resolved_host_session_id()
    if not sid:
        return
    try:
        from lemoncrow.core.foundation.paths import session_dir

        seg_dir = session_dir(_lemoncrow_root(), host or _detect_agent(), sid)
        from lemoncrow.core.capabilities.savings_summary import savings_frames

        frames = savings_frames(session_id=sid)
        if frames:
            seg_dir.mkdir(parents=True, exist_ok=True)
            # All frames, one per line: statusline.sh picks by wall clock so
            # rotation continues BETWEEN sidecar writes instead of freezing on
            # whichever single frame was current at write time.
            (seg_dir / "statusline_frames").write_text("\n".join(frames) + "\n", encoding="utf-8")
            # Legacy single-frame sidecar for older installed statusline.sh.
            idx = int(time.time() // 5) % len(frames)
            (seg_dir / "statusline_segment").write_text(frames[idx], encoding="utf-8")
    except Exception:
        _log.debug("statusline sidecar write failed", exc_info=True)


_dev_mode_cache: bool | None = None


def _mcp_debug_enabled() -> bool:
    if os.environ.get("LEMONCROW_MCP_DEBUG", "0") not in ("0", "", "false", "no"):
        return True
    # Auto-enable in dev installations (marker written by make dev / scripts/local.sh).
    # The marker is constant per process, so cache the stat instead of issuing
    # a syscall on every tool call.
    global _dev_mode_cache
    if _dev_mode_cache is None:
        try:
            _dev_mode_cache = (_lemoncrow_root() / ".dev_mode").exists()
        except Exception:
            _dev_mode_cache = False
    return _dev_mode_cache


def _mcp_debug_path(session_id: str = "") -> Path:
    """Return the per-session debug log path.

    When a session_id is known, writes land at::

        ~/.lemoncrow/sessions/<session_id>/mcp_debug.jsonl

    so each Claude Code session has its own isolated log that can be read,
    tailed, or deleted independently.  Falls back to a top-level
    ``mcp_debug_unknown.jsonl`` for the rare case where the session id is not
    yet resolved (early boot calls).
    """
    root = _lemoncrow_root()
    if session_id:
        from lemoncrow.core.foundation.paths import session_dir

        return session_dir(root, _detect_agent(), session_id) / "mcp_debug.jsonl"
    return root / "mcp_debug_unknown.jsonl"


# Argument keys whose values carry credentials/PII (DSNs, tokens, passwords).
# These are masked regardless of length before anything is written to the debug
# log or shipped to telemetry.
_DEBUG_SECRET_KEY_RE = re.compile(
    r"(connection_string|dsn|api[_-]?key|token|secret|password|authorization)",
    re.IGNORECASE,
)

# Keys whose large values are still truncated in the *telemetry* path
# (Langfuse / OTel) to keep event payloads small.  The local mcp_debug.jsonl
# always logs full content — only secrets are redacted there.
_DEBUG_LARGE_KEYS = frozenset({"new_string", "old_string", "content", "prompt", "task", "query"})


def _scrub_args_for_debug(args: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool args before logging or telemetry emission.

    Secrets (connection_string / dsn / api_key / token / secret / password /
    authorization) are always masked, regardless of value length.
    """

    def _scrub_value(key: str | None, value: Any) -> Any:
        if isinstance(key, str) and _DEBUG_SECRET_KEY_RE.search(key):
            return "<redacted>"
        if isinstance(value, dict):
            return {kk: _scrub_value(kk, vv) for kk, vv in value.items()}
        if isinstance(value, list):
            return [_scrub_value(None, item) for item in value]
        return value

    return {k: _scrub_value(k, v) for k, v in args.items()}


# Cleanup: remove mcp_debug.jsonl files from sessions older than this many days.
_MCP_DEBUG_RETENTION_DAYS = 7
# Track last prune time to avoid pruning every single tool call.
_mcp_debug_last_prune: float = 0.0
_MCP_DEBUG_PRUNE_INTERVAL_S = 3600.0  # prune at most once per hour


def _prune_mcp_debug_logs() -> None:
    """Delete per-session mcp_debug.jsonl files older than _MCP_DEBUG_RETENTION_DAYS.

    Runs at most once per hour (guarded by ``_mcp_debug_last_prune``).  Only
    the debug file itself is removed — the session directory and its other
    artefacts (savings.jsonl, etc.) are left untouched.  Fail-open.
    """
    global _mcp_debug_last_prune
    now = time.time()
    if now - _mcp_debug_last_prune < _MCP_DEBUG_PRUNE_INTERVAL_S:
        return
    _mcp_debug_last_prune = now
    try:
        sessions_dir = _lemoncrow_root() / "sessions"
        if not sessions_dir.is_dir():
            return
        cutoff = now - _MCP_DEBUG_RETENTION_DAYS * 86400
        for debug_file in sessions_dir.glob("*/mcp_debug.jsonl"):
            try:
                if debug_file.stat().st_mtime < cutoff:
                    debug_file.unlink(missing_ok=True)
            except Exception:
                pass
        # Also prune the fallback unknown-session file if it is old.
        unknown = _lemoncrow_root() / "mcp_debug_unknown.jsonl"
        try:
            if unknown.exists() and unknown.stat().st_mtime < cutoff:
                unknown.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception:
        pass  # never disrupt the server


def _tool_profile_path() -> Path | None:
    """Per-run tool-latency sink, or None when profiling is off.

    Set ``LEMONCROW_TOOL_PROFILE_PATH`` to a writable JSONL path (the benchmark
    points it at a file next to each run's .flow capture) to record per-call
    timing scoped to a single run -- unlike the global ``mcp_debug.jsonl``, which
    mixes every workspace and session. Unset in production -> no-op, no I/O.
    """
    raw = os.environ.get("LEMONCROW_TOOL_PROFILE_PATH", "").strip()
    return Path(raw) if raw else None


def _append_tool_profile(
    *,
    tool: str,
    handler_ms: int,
    total_ms: int,
    response_size: int,
    status: str,
    session_id: str = "",
    error: str | None = None,
) -> None:
    """Append one per-call latency record to the per-run profile sink.

    ``handler_ms`` is the tool handler itself; ``total_ms`` covers the whole
    dispatch incl. the post-handler pipeline (render/dedup/spill/compact/token
    ledger), so ``overhead_ms = total_ms - handler_ms`` exposes server-side cost
    the handler-only timer misses. One sub-4KB line per call -> POSIX-atomic
    append, safe across the server's worker threads. Fail-open.
    """
    path = _tool_profile_path()
    if path is None:
        return
    try:
        entry: dict[str, Any] = {
            "ts": time.time(),
            "tool": tool,
            "handler_ms": handler_ms,
            "total_ms": total_ms,
            "overhead_ms": max(0, total_ms - handler_ms),
            "response_size": response_size,
            "status": status,
            "session_id": session_id,
        }
        if error:
            entry["error"] = error
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass  # never disrupt the server


def _append_mcp_debug_event(
    *,
    tool: str,
    args: dict[str, Any],
    duration_ms: int,
    response_size: int,
    status: str,
    error: str | None = None,
    session_id: str = "",
    rid: str | None = None,
) -> None:
    """Write a per-call debug record to sessions/<session_id>/mcp_debug.jsonl.

    Only active when LEMONCROW_MCP_DEBUG=1 (or in dev-mode installs).  Each
    session writes to its own file so logs are isolated, greppable per-session,
    and cleaned up independently.  Args are fully logged (only credentials are
    redacted) so the debug log is genuinely useful.  Fail-open.
    """
    if not _mcp_debug_enabled():
        return
    try:
        entry: dict[str, Any] = {
            "ts": time.time(),
            "tool": tool,
            "mcp_tool": f"mcp__{SERVER_NAME}__{tool}",
            "args": _scrub_args_for_debug(args),
            "duration_ms": duration_ms,
            "response_size_bytes": response_size,
            "status": status,
            "session_id": session_id,
        }
        if rid is not None:
            entry["rid"] = rid
        if error:
            entry["error"] = error
        path = _mcp_debug_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        # Opportunistic cleanup -- at most once per hour, non-blocking.
        _prune_mcp_debug_logs()
    except Exception:
        pass  # never disrupt the server


# smart_state read/write moved to mcp.smart_state (imported near the top).


# bash symbols moved to mcp.bash (imported/registered near the top).


# smart_state flock moved to mcp.smart_state (imported near the top).


def _record_smart_state_savings(tokens_saved: int, calls_avoided: int) -> None:
    if tokens_saved <= 0 and calls_avoided <= 0:
        return
    # Serialize the read-modify-write across threads (_STATE_LOCK) AND sibling
    # MCP processes sharing the machine-global smart_state.json (flock), so a
    # concurrent process can't lose-update the cumulative counters.
    with _STATE_LOCK:
        _flock = _acquire_smart_state_flock()
        try:
            state = _read_smart_state()
            savings = state.get("savings")
            if not isinstance(savings, dict):
                savings = {"calls_avoided": 0, "tokens_saved": 0}
            savings["calls_avoided"] = int(savings.get("calls_avoided", 0) or 0) + max(0, calls_avoided)
            savings["tokens_saved"] = int(savings.get("tokens_saved", 0) or 0) + max(0, tokens_saved)
            state["savings"] = savings
            _write_smart_state(state)
        finally:
            _release_smart_state_flock(_flock)


# ── Per-command bash spend ledger (`lc audit bash`) ─────────────────────
# For every normalized bash command family, accumulate how many chars its
# output still SHIPPED into context after all compaction next to how many it
# OMITTED. The top shipped rows are the compaction gaps worth new filters --
# the `rtk discover` equivalent, built from LemonCrow's own ledger.
# bash symbols moved to mcp.bash (imported/registered near the top).
# bash symbols moved to mcp.bash (imported/registered near the top).
# bash symbols moved to mcp.bash (imported/registered near the top).
# bash symbols moved to mcp.bash (imported/registered near the top).
# bash symbols moved to mcp.bash (imported/registered near the top).
# bash symbols moved to mcp.bash (imported/registered near the top).


# bash symbols moved to mcp.bash (imported/registered near the top).


# bash symbols moved to mcp.bash (imported/registered near the top).


class _NoOpContextBudgetRecorder:
    """No-op recorder for service-backed MCP state."""

    def record(self, **kwargs: Any) -> None:
        pass

    def record_compact_tool_output(self, **kwargs: Any) -> None:
        pass

    def aggregate_run(self, session_id: str) -> Any:
        return {}


def _get_context_budget_recorder() -> Any:
    global _context_budget_recorder
    if _service_backed_state():
        return _NoOpContextBudgetRecorder()
    if _context_budget_recorder is None:
        try:
            from lemoncrow.core.capabilities.telemetry.context_budget import ContextBudgetRecorder
            from lemoncrow.infra.storage.factory import create_store

            store = create_store(_lemoncrow_root())
            store.init()
            _context_budget_recorder = ContextBudgetRecorder(store)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            _context_budget_recorder = _NoOpContextBudgetRecorder()
    return _context_budget_recorder


def _core_runtime() -> Any:
    return _runtime().core_runtime


def _memory_store() -> Any:
    """Compatibility wrapper around the host-neutral per-thread memory store."""
    return memory_store(_lemoncrow_root())


def _archival_recall() -> ArchivalRecallCapability:
    from lemoncrow.infra.embeddings.factory import make_embedder
    from lemoncrow.pro.capabilities.archival_recall import ArchivalRecallCapability

    return ArchivalRecallCapability(_memory_store(), make_embedder(), redactor=redact)


def _symbol_recall() -> Any:
    from lemoncrow.core.foundation.history_store import HistoryStore
    from lemoncrow.pro.capabilities.archival_recall.symbol_recall import SymbolRecallCapability

    workspace_root = _workspace_root()
    trace_store = HistoryStore(_lemoncrow_root())
    trace_store.init()
    return SymbolRecallCapability(
        repo_root=workspace_root,
        engine=_code_context_engine(str(workspace_root)),
        memory_store=_memory_store(),
        trace_store=trace_store,
    )


_last_session_cwd: str | None = None
"""Most recent explicit ``cwd`` seen on a ``bash`` call, or None.

This server is a long-lived process whose own ``os.getcwd()`` is fixed at
launch, and MCP carries no per-call session cwd (we implement no ``roots``
capability). So when a session enters a git worktree, :func:`_workspace_root`
keeps naming the *main checkout*. A bash call's explicit ``cwd`` is the only
place the session's real working directory reaches this process, which is why
bash honored the worktree and edit did not. Recorded here and used to resolve
relative edit paths -- see :func:`_session_worktree_root`.

lc-debt: process-global, so a shared HTTP-mode server hosting two sessions on
the SAME repo could let one session's worktree cwd redirect the other's
relative edits (a foreign repo is already rejected by the
``<root>/.git/worktrees`` check, and the redirect is always disclosed).
Upgrade path: key this by MCP session id once the dispatcher threads one
through, or drop it entirely if the protocol ever carries a per-call cwd.
"""


def _record_session_cwd(name: str, args: Any) -> None:
    """Remember the most recent explicit bash cwd (compat state wrapper)."""
    global _last_session_cwd
    _last_session_cwd = _next_session_cwd(_last_session_cwd, name, args)


def _session_worktree_root(workspace_root: Path) -> Path | None:
    """Resolve the recorded session cwd to a linked worktree of this workspace."""
    return _canonical_session_worktree_root(_last_session_cwd, workspace_root)


def _review_handler_hooks() -> ReviewHandlerHooks:
    return ReviewHandlerHooks(
        resolved_host_session=_resolved_host_session,
        get_mcp_model=_get_mcp_model,
        workspace_root=_workspace_root,
        session_worktree_root=_session_worktree_root,
    )


configure_review_handler_hooks(_review_handler_hooks)


# Thread-local slot for passing real tokens_saved from tool handlers to the
# budget recorder without polluting the LLM-facing response dict.
# _tool_call_tokens_saved moved to mcp.smart_state (imported/re-exported).
# Per-call accounting/render/raw-result/image state lives in
# ``lemoncrow.gateway.tools.state`` and is re-exported above for compatibility.
def _bootstrap_context_status(root: Path) -> dict[str, Any]:
    from lemoncrow.core.service.bootstrap_context import bootstrap_status, missing_bootstrap_labels
    from lemoncrow.core.service.jobs import JOB_BOOTSTRAP_CONTEXT
    from lemoncrow.infra.storage.factory import create_store
    from lemoncrow.pro.capabilities.code_context import CodeContextEngine

    repo_root = _workspace_root().resolve()
    repo_id = CodeContextEngine(repo_root).repo_id
    memory_store = _memory_store()
    state = bootstrap_status(memory_store, repo_id)
    store = create_store(root)
    store.init()
    jobs = [
        job
        for job in store.jobs.list_jobs(job_type=JOB_BOOTSTRAP_CONTEXT, limit=200)
        if isinstance(job.get("payload"), dict) and job["payload"].get("repo_id") == repo_id
    ]
    queued = False
    # Only block re-queueing if there is an already-active (pending or running) job.
    # Failed/dead jobs should not permanently prevent retrying bootstrap.
    active_job = next((job for job in jobs if job["status"] in {"pending", "running"}), None)
    job_id: str | None = None
    if state != "warm" and active_job is None:
        job_id = store.jobs.enqueue_job(
            JOB_BOOTSTRAP_CONTEXT,
            {"repo_root": str(repo_root), "repo_id": repo_id},
        )
        queued = True
    status = "warm" if state == "warm" else ("warming" if queued or active_job or job_id else state)
    return {
        "repo_id": repo_id,
        "queued": queued,
        "job_id": job_id,
        "status": status,
        "missing_labels": missing_bootstrap_labels(memory_store, repo_id),
    }


def _context_handler_hooks() -> ContextHandlerHooks:
    return ContextHandlerHooks(
        code_context_engine=_code_context_engine,
        scoped_context_capability=_scoped_context_capability,
        runtime=_runtime,
        get_ledger=_get_ledger,
        match_mcp_lexical=_match_mcp_lexical,
        bootstrap_context_status=_bootstrap_context_status,
        lemoncrow_root=_lemoncrow_root,
        workspace_root=_workspace_root,
        advance_monitors=_advance_monitors,
        get_product_session_id=_get_product_session_id,
        spawn_worker_if_idle=_spawn_worker_if_idle,
        debug_enabled=_mcp_debug_enabled,
    )


configure_context_handler_hooks(_context_handler_hooks)


def _prefix_cache_diagnostics_from_ledger(led: Any) -> dict[str, Any]:
    """Extract prefix cache metrics from recorded llm_call events in the ledger."""
    call_events = [e for e in led.events if e.payload.get("kind") == "llm_call"]
    if not call_events:
        return {
            "turn_count": 0,
            "cache_hit_ratio": 0.0,
            "cache_read_tokens_saved": 0,
            "avg_prefix_tokens": 0,
            "avg_dynamic_tokens": 0,
            "current_prefix_hash": "",
            "prefix_invalidated_reason": "",
        }

    cache_read_totals = [int(e.payload.get("cache_read_tokens", 0)) for e in call_events]
    modeled_cache_read_totals = [int(e.payload.get("modeled_cache_read_tokens", 0)) for e in call_events]
    input_totals = [int(e.payload.get("input_tokens", 0)) for e in call_events]
    prefix_hashes = [e.payload.get("stable_prefix_hash", "") for e in call_events]

    # A turn is a cache "hit" when cache_read_tokens > 0
    eligible = call_events[1:]
    hits = sum(1 for e in eligible if int(e.payload.get("cache_read_tokens", 0)) > 0)
    hit_ratio = round(hits / len(eligible), 4) if eligible else 0.0
    cache_read_saved = sum(cache_read_totals)
    avg_input = int(sum(input_totals) / len(input_totals)) if input_totals else 0

    last = call_events[-1]
    return {
        "turn_count": len(call_events),
        "cache_hit_ratio": hit_ratio,
        "cache_read_tokens_saved": cache_read_saved,
        "modeled_cache_read_tokens_saved": sum(modeled_cache_read_totals),
        "avg_prefix_tokens": avg_input,
        "avg_dynamic_tokens": 0,
        "current_prefix_hash": prefix_hashes[-1] if prefix_hashes else "",
        "prefix_invalidated_reason": last.payload.get("prefix_invalidated_reason", ""),
    }


def _rescue_handler_hooks() -> RescueHandlerHooks:
    return RescueHandlerHooks(
        runtime=_runtime,
        get_ledger=_get_ledger,
        match_mcp_lexical=_match_mcp_lexical,
        get_product_session_id=_get_product_session_id,
    )


configure_rescue_handler_hooks(_rescue_handler_hooks)


def _trace_handler_hooks() -> TraceHandlerHooks:
    return TraceHandlerHooks(
        runtime=_runtime,
        get_ledger=_get_ledger,
        get_realtime_context=_get_realtime_context,
        get_product_session_id=_get_product_session_id,
        memory_store=_memory_store,
        spawn_worker_if_idle=_spawn_worker_if_idle,
        lemoncrow_root=_lemoncrow_root,
        max_trace_files=_MAX_TRACE_FILES,
        max_trace_file_bytes=_MAX_TRACE_FILE_BYTES,
    )


configure_trace_handler_hooks(_trace_handler_hooks)


def _compress_context(session_id: str | None = None) -> Any:
    """Compress the current ledger state into a compact prompt block for context continuation.

    Call when context is heavy; the block preserves decisions and state while dropping stale history.

    Returns: {prompt_block, tokens_before, tokens_after_estimate, tokens_freed, cost_saved_usd}.
    """
    from lemoncrow.pro.runtime.context_compressor import ContextCompressor

    led = _get_ledger()
    if session_id:
        led.session_id = session_id
    state = ContextCompressor().compress(led, preserve_last_n_turns=10, workspace_root=_workspace_root())
    compaction_savings = _session_compaction_savings_payload(
        led,
        state,
        tokens_before=int(led.token_count or 0),
        trigger="compact_session",
        reason="session compaction executed",
    )
    if int(compaction_savings["tokens_saved"]) > 0:
        _append_live_savings_event(compaction_savings)

    with contextlib.suppress(Exception):
        from lemoncrow.pro.runtime import outcome_capture

        outcome_capture.schedule_compact(
            session_id=led.session_id,
            trigger="compact_session",
            tokens_before=int(compaction_savings["tokens_before"]),
            tokens_after=int(compaction_savings["tokens_after_estimate"]),
            must_keep_keywords=list(led.active_playbooks),
            errors_before=len(led.errors_seen) + len(led.repeated_failures),
            writer=_make_outcome_writer(led),
        )

    return {
        "prompt_block": state.to_prompt_block(),
        "tokens_before": int(compaction_savings["tokens_before"]),
        "tokens_after_estimate": int(compaction_savings["tokens_after_estimate"]),
        "tokens_freed": int(compaction_savings["tokens_freed"]),
        "cost_saved_usd": float(compaction_savings["cost_saved_usd"]),
    }


def _compact_handler_hooks() -> CompactHandlerHooks:
    return CompactHandlerHooks(compress_context=_compress_context)


configure_compact_handler_hooks(_compact_handler_hooks)


def _memory_upsert_block(
    agent_id: str,
    label: str,
    value: str,
    limit_chars: int = 8000,
    description: str = "",
    read_only: bool = False,
    pinned: bool = False,
    metadata: dict[str, Any] | None = None,
    expected_version: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for host-neutral editable MemoryBlock upsert."""
    return _memory_service().upsert_editable_block(
        agent_id=agent_id,
        label=label,
        value=value,
        limit_chars=limit_chars,
        description=description,
        read_only=read_only,
        pinned=pinned,
        metadata=metadata,
        expected_version=expected_version,
        actor=actor,
        field_redactor=_redact_memory_input,
    )


def _memory_get_block(agent_id: str | None, label: str) -> dict[str, Any] | None:
    """Retrieve a MemoryBlock by label."""
    return _get_memory_block_impl(_memory_store(), agent_id, label)


def _memory_archive(
    agent_id: str | None,
    text: str,
    source: str,
    source_ref: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Archive long-term memory text for later recall."""
    return _archive_memory_impl(
        _archival_recall(),
        agent_id=agent_id,
        text=text,
        source=source,
        source_ref=source_ref,
        tags=tags,
    )


def _memory_recall(
    agent_id: str | None,
    query: str,
    top_k: int = 5,
    tags: list[str] | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Recall durable memory plus past-session passages."""
    return _recall_memory_impl(
        _memory_service(),
        agent_id=agent_id,
        query=query,
        top_k=top_k,
        tags=tags,
        since=since,
        session_passages=lambda q, k: _session_recall_passages(q, k),
    )


def _session_recall_passages(query: str, top_k: int) -> list[dict[str, Any]]:
    """Past-session recall hits shaped like MemoryRecallPassage records."""
    return _session_recall_passages_impl(_lemoncrow_root(), query, top_k)


def _memory_service() -> MemoryService:
    return memory_service(store=_memory_store())


def _memory_store_fact(
    *,
    agent_id: str | None,
    subject: str,
    fact: str,
    citations: str,
    reason: str,
    scope: str,
) -> dict[str, Any]:
    """Store a durable fact with Copilot-memory-like fields in LemonCrow memory."""
    return _store_memory_fact_impl(
        _memory_service(),
        agent_id=agent_id,
        subject=subject,
        fact=fact,
        citations=citations,
        reason=reason,
        scope=scope,
        field_redactor=_redact_memory_input,
    )


def _memory_vote_fact(
    *,
    agent_id: str | None,
    fact: str,
    direction: str,
    reason: str,
    scope: str | None,
) -> dict[str, Any]:
    """Vote on an existing stored fact by exact fact text."""
    return _vote_memory_fact_impl(
        _memory_service(),
        agent_id=agent_id,
        fact=fact,
        direction=direction,
        reason=reason,
        scope=scope,
        field_redactor=_redact_memory_input,
    )


def _memory_handler_hooks() -> MemoryHandlerHooks:
    return MemoryHandlerHooks(
        recall=_memory_recall,
        store_fact=_memory_store_fact,
        vote_fact=_memory_vote_fact,
        symbol_recall=_symbol_recall,
    )


configure_memory_handler_hooks(_memory_handler_hooks)


def render_tool_result_text(name: str, result: Any) -> str | None:
    """Compatibility wrapper using the live MCP bash execution policy value."""
    from lemoncrow.gateway.adapters.mcp import bash as _mcp_bash

    return _canonical_render_tool_result_text(
        name,
        result,
        bash_idle_grace_s=_mcp_bash._BASH_IDLE_GRACE_S,
    )


# Within a contiguous run, re-anchor the gutter every K lines. K bounds the
# offset arithmetic a model must do to name any line (≤4 lines from the
# nearest anchor above) — small enough that :Lx-Ly edit targets stay read-off
# rather than computed, so the range-edit path doesn't lose to old/new on
# friction. K=5 keeps ~80% of the full-gutter savings.


# Same "not a literal path" shape test as the code-context engine's
# _query_is_pathy_literal: whitespace or a regex/FTS metacharacter means the
# query can't be a literal filesystem path, so skip the stat/walk below.
_QUERY_PATH_HINT_RE = re.compile(r"[\s|*()\[\]^$+?\\=<>{}]")


def _candidate_relpaths_for_query(query: str) -> list[str]:
    """Path forms worth an existence check for a path-shaped query: as given
    (leading slash stripped), and with one leading segment dropped -- a
    phantom repo-name prefix (e.g. "/lemoncrow/benchmarks/x.sh" when the real
    relpath is "benchmarks/x.sh") is a common way an agent mistypes a path.
    """
    stripped = query.strip().lstrip("/")
    candidates = [stripped] if stripped else []
    parts = stripped.split("/")
    if len(parts) > 1:
        trimmed = "/".join(parts[1:])
        if trimmed and trimmed not in candidates:
            candidates.append(trimmed)
    return candidates


def _lookup_unique_basename_in_index(engine: Any, basename: str) -> str | None:
    """Unique indexed file whose basename is `basename`, via the code index's
    `files` table -- a couple ms and deterministic, unlike a live filesystem
    walk over a large repo (the `read`-tool suggestion helper below hits its
    250ms/20k-file budget on this repo before finishing the walk, so which
    file it lands on -- if any -- depends on OS directory-iteration order).
    """
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{engine.db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT file_path FROM files WHERE repo_id = ? AND (file_path = ? OR file_path LIKE '%/' || ?) LIMIT 2",
            (engine.repo_id, basename, basename),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return rows[0][0] if len(rows) == 1 else None


def _resolve_query_as_existing_file(workspace_root: Path, query: str, engine: Any = None) -> str | None:
    """When `query` names an existing repo file, return its workspace-relative
    path so code_search can pin straight to it instead of ranking.

    Tries the query verbatim as a relpath (and with one bogus leading segment
    stripped), then -- when `engine` is given -- falls back to a unique
    basename lookup against the code index. None when the query isn't
    path-shaped or doesn't resolve to exactly one file.
    """
    q = query.strip()
    if not q or _QUERY_PATH_HINT_RE.search(q) or ("/" not in q and "." not in q):
        return None
    root = workspace_root.resolve()
    for candidate in _candidate_relpaths_for_query(q):
        target = (root / candidate).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue  # escapes the workspace, e.g. "../../etc/passwd"
        if target.is_file():
            return str(target.relative_to(root))
    if engine is not None:
        basename = Path(q).name
        if basename:
            return _lookup_unique_basename_in_index(engine, basename)
    return None


def _read_summary_response(resolved: Path) -> dict[str, Any]:
    """Compatibility wrapper around the canonical bounded summary policy."""
    return _canonical_read_summary_response(
        resolved,
        semantic_memory_root=_lemoncrow_root(),
        render_outline=_render_read_outline_md,
    )


# Freshness ledger for blind (no-old) range edits: abs path -> (mtime_ns, size)
# stat signature captured whenever `read`/`code_search` serves that file's
# content. A matching signature proves the requesting model saw the file as it
# currently is on disk, i.e. its :Lx-Ly line numbers still index the same
# bytes. Scoped PER SESSION, mirroring _http_session_ledgers: on the
# multi-client HTTP transport each session's reads prove freshness only for
# that session's own blind range edits (client B can no longer pass the
# staleness check on client A's read). The stdio transport has no request
# ledger and keeps one process-wide "_global" bucket -- one MCP server per
# host window, so that state stays exactly window-scoped. Never refreshed on
# edit-writes -- the entry stays pinned to the serve the model actually read,
# which is exactly the snapshot a later range edit must be relocated against.
# Third element: whether this serve gave line numbers that match disk exactly
# (an expand/:full or explicit :Lx-Ly range) vs a lossy projection (bare
# default read -> minified/compact, or :outline/:summary) whose line numbers
# do NOT index the same bytes. A blind range edit built from the latter would
# silently splice the wrong lines.
# Fourth element: a per-line digest snapshot of the file AS SERVED (empty when
# not captured -- lossy serve, oversized file, unreadable). It turns the guard
# from "did this file change at all" into "did the lines this edit names
# change": drift somewhere else in the file no longer rejects the edit, and a
# pure line SHIFT is repaired by locating the served block at its new offset.
# Per-session recent code_search queries, same session-bucketing rationale as
# _RANGE_READ_SIGS above (HTTP: per client session; stdio: one process-wide
# "_global" bucket per host window). Feeds _check_repeat_query's near-
# duplicate nudge -- measured 11 near-duplicate code_search calls in a single
# debt-benchmark rep (2026-07-25): "fail-on-stale stale-days debt_cmd",
# "stale_days fail_on_stale debt stale age git blame", "_line_age_days git
# blame annotate stale", ... -- each re-phrasing re-sent on every later turn's
# resent history, compounding for the rest of the run.
# Timestamped (not just a bare deque): the stdio "_global" bucket is process-
# wide, so it spans EVERY conversation a long-lived daemon happens to serve
# (a daemon can outlive many separate Cursor/Claude chats -- confirmed this
# session). Without a time bound, a brand-new, unrelated conversation whose
# first query happens to share words with the LAST daemon-wide query (from a
# previous, unrelated chat, possibly hours earlier) would be wrongly blanked.
# Bounding to _RECENT_QUERY_WINDOW_SECONDS keeps the check scoped to "this
# same burst of searching", not "this process's entire lifetime".
_RECENT_CODE_SEARCH_QUERIES: OrderedDict[str, deque[tuple[float, str, frozenset[str]]]] = OrderedDict()
_recent_code_search_queries_lock = threading.Lock()
_MAX_RECENT_QUERY_SESSIONS = _MAX_HTTP_SESSION_LEDGERS
_RECENT_QUERY_HISTORY = 3
_REPEAT_QUERY_SIMILARITY_FLOOR = 0.35
_RECENT_QUERY_WINDOW_SECONDS = 180.0
_QUERY_WORD_RE = re.compile(r"[a-z0-9]+")


def _query_words(query: str) -> frozenset[str]:
    """Lowercased alnum tokens of length >=3, for cheap query-similarity."""
    return frozenset(w for w in _QUERY_WORD_RE.findall(query.lower()) if len(w) >= 3)


def _recent_code_search_queries() -> deque[tuple[float, str, frozenset[str]]]:
    """The current session's recent-query bucket (see _RECENT_CODE_SEARCH_QUERIES)."""
    led = getattr(_request_ledger, "value", None)
    sid = led.session_id if isinstance(led, RunLedger) and led.session_id else "_global"
    with _recent_code_search_queries_lock:
        bucket = _RECENT_CODE_SEARCH_QUERIES.get(sid)
        if bucket is None:
            if len(_RECENT_CODE_SEARCH_QUERIES) >= _MAX_RECENT_QUERY_SESSIONS:
                _RECENT_CODE_SEARCH_QUERIES.popitem(last=False)
            bucket = deque(maxlen=_RECENT_QUERY_HISTORY)
            _RECENT_CODE_SEARCH_QUERIES[sid] = bucket
        else:
            _RECENT_CODE_SEARCH_QUERIES.move_to_end(sid)
        return bucket


def _check_repeat_query(query: str) -> bool:
    """True when *query* overlaps heavily (Jaccard >= floor) with a query this
    session ran within the last _RECENT_QUERY_WINDOW_SECONDS -- re-phrasing the
    same investigation rarely surfaces anything new. A hard signal, not a
    hint: an explanatory sentence here just adds tokens to a response the
    caller has already shown it skims past and searches again anyway (measured
    across debt-benchmark reps). The caller returns blank results instead --
    always records *query* into the bucket either way. Time-bounded (not just
    the last _RECENT_QUERY_HISTORY calls) so a brand-new, unrelated
    conversation on the same long-lived daemon never gets blanked because of a
    coincidental word overlap with a stale query from a previous chat.
    """
    words = _query_words(query)
    bucket = _recent_code_search_queries()
    now = time.monotonic()
    is_repeat = False
    if words:
        for ts, _prior_text, prior_words in bucket:
            if now - ts > _RECENT_QUERY_WINDOW_SECONDS:
                continue
            union = words | prior_words
            if not union:
                continue
            if len(words & prior_words) / len(union) >= _REPEAT_QUERY_SIMILARITY_FLOOR:
                is_repeat = True
                break
    bucket.append((now, query, words))
    return is_repeat


def _smart_read_single(
    path: str,
    range: str | None = None,
    expand: bool = False,
    max_lines: int | None = None,
    tail_lines: int | None = None,
    include_meta: bool = False,
    projection_kind: str | None = None,
    summary: bool = False,
    outline: bool = False,
) -> dict[str, Any]:
    """Execute a single-file smart-read.  Called by both the decorated tool and the batch loop.

    View precedence when `expand`/`range`/`outline`/`summary` combine: expand >
    range > outline > summary (most detailed wins; see the resolution below).
    """
    from lemoncrow.pro.capabilities.source_projection import (
        CompactProjectionResult,
        MinifiedProjectionResult,
        SourceProjection,
    )

    target_path = path
    if not target_path:
        raise ValueError("provide path")
    # Support a trailing ":Lx-Ly" line-range suffix on the path
    # itself (e.g. "store.py:L60-L100"); an explicit range= argument wins.
    target_path, suffix_range = _split_read_range_suffix(target_path)
    if range is None and suffix_range is not None:
        range = suffix_range
    # `:full`, an explicit range (suffix or argument), `:outline`, and
    # `:summary` name four whole/partial-file views of increasing detail.
    # Rather than reject a combination as ambiguous, resolve it by PRECEDENCE
    # -- expand > range > outline > summary -- because the calling LLM can
    # always summarize DOWN from a more detailed result for free, but
    # recovering UP from an over-reduced one costs another turn. Downgrading
    # the less-detailed request silently is safe: the response's own `mode`
    # field says which view was actually served.
    if expand:
        outline = False
        summary = False
    elif range is not None:
        outline = False
        summary = False
    elif outline:
        summary = False
    # tail=N: resolve to a concrete line range so the rest of the read path
    # handles it uniformly (range read is already bounded and efficient).
    if tail_lines is not None and range is None and not expand:
        try:
            total = sum(1 for _ in open(_workspace_path(target_path), encoding="utf-8", errors="replace"))
            start = max(1, total - tail_lines + 1)
            range = f"{start}-"
        except OSError:
            pass
    # Reads may target any path the host process can access — a coding agent
    # legitimately reads configs / sibling repos outside the project, and the
    # host's own permission layer gates the tool call. Writes/edits, by contrast,
    # are confined to the workspace (see tool_smart_edit). Relative paths still
    # resolve against the workspace root.
    resolved = _workspace_path(target_path)
    # Freshness ledger: recorded below, once the actual view served (exact vs
    # a lossy projection) is known -- see _record_read_sig's exact= kwarg.
    # A ranged read is served EXACTLY as requested -- never silently widened.
    # (A "3 partial reads -> serve the whole file" escalation used to live here;
    # it misfired on scattered spot-checks and dumped multi-thousand-line files
    # the caller never asked for. Precise slice > vague complete source.)
    # Binary / non-text guard: never silently UTF-8-decode a binary file into
    # mojibake (a PNG used to come back as garbage, which forced agents into
    # pixel-processing hacks). Sniff the head; if it is not valid UTF-8 text,
    # return a structured signal instead of decoding. Images especially must be
    # recognized as such, not mangled into text.
    if resolved.is_file():
        try:
            with open(resolved, "rb") as _bfh:
                _sniff = _bfh.read(8192)
        except OSError:
            _sniff = b""
        _binary = b"\x00" in _sniff
        if _sniff and not _binary:
            try:
                _sniff.decode("utf-8")
            except UnicodeDecodeError:
                # tolerate a multibyte char split at the 8 KiB sniff boundary
                try:
                    _sniff[:-4].decode("utf-8")
                except UnicodeDecodeError:
                    _binary = True
        if _binary and b"\x00" not in _sniff:
            import mimetypes as _mtypes

            # A text-typed file with stray non-UTF8 bytes but no NULs (a build
            # log with raw control bytes, a latin-1 .txt) is mixed text, not a
            # true binary: serve it lossy-decoded (downstream readers open with
            # errors="replace") instead of a "not decoded" dead-end that
            # measurably sent agents into cat/head/wc/strings archaeology.
            if (_mtypes.guess_type(str(resolved))[0] or "").startswith("text/"):
                _binary = False
        if _binary:
            import mimetypes

            _mt = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            try:
                _sz = resolved.stat().st_size
            except OSError:
                _sz = len(_sniff)
            if _mt.startswith("image/") and 0 < _sz <= _MAX_INLINE_IMAGE_BYTES:
                # Hand the actual image to the multimodal model as an MCP image
                # content block (drained into the response `content` by the
                # tools/call dispatcher), so it can read the image directly rather
                # than resorting to pixel-processing hacks.
                try:
                    import base64

                    _b64 = base64.b64encode(resolved.read_bytes()).decode("ascii")
                    _imgs = getattr(_tool_call_images, "value", None)
                    if not isinstance(_imgs, list):
                        _imgs = []
                        _tool_call_images.value = _imgs
                    _imgs.append({"type": "image", "data": _b64, "mimeType": _mt})
                    return {
                        "mode": "image",
                        "path": str(resolved),
                        "media_type": _mt,
                        "bytes_total": _sz,
                        "message": f"Image ({_mt}, {_sz} bytes) attached for viewing.",
                    }
                except OSError:
                    pass
            _msg = _binary_read_message(_mt, _sz, suffix=resolved.suffix.lower())
            return {
                "mode": "binary",
                "path": str(resolved),
                "media_type": _mt,
                "bytes_total": _sz,
                "message": _msg,
            }
    if summary and resolved.is_file():
        return _read_summary_response(resolved)
    if max_lines is not None and range is None and not expand:
        payload = cast(dict[str, Any], _core_runtime().smart_read(str(resolved), max_lines=max_lines))
        payload.setdefault("mode", "summary")
        payload["projection"] = SourceProjection.summary().to_dict()
        if include_meta:
            return payload
        payload.pop("cache_hit", None)
        payload.pop("tokens_saved", None)
        return payload

    target = resolved

    # Detect directory input early — return a helpful listing instead of a cryptic error.
    if target.is_dir():
        try:
            entries = sorted(
                os.listdir(target),
                key=lambda x: (not (target / x).is_dir(), x.lower()),
            )
        except OSError:
            entries = []
        return {
            "mode": "directory",
            "path": str(target),
            "entries": [(e + "/" if (target / e).is_dir() else e) for e in entries],
            "message": "Directory, not a file. Use `search` (by name) or `grep` (file_glob_patterns) to find files.",
        }

    # Source-side guard: an exact full read (expand) of a very large file would
    # load the whole file into memory and serialize into one oversized JSON-RPC
    # frame, which disconnects the host. Read only a bounded prefix from disk so
    # gigabytes are never materialized. Range reads are inherently bounded, and
    # non-expand reads of code files become cheap outlines, so neither is touched.
    if expand and range is None:
        disconnect_cap = _max_result_bytes()
        inline_budget = _read_inline_budget_bytes()
        try:
            total_bytes = target.stat().st_size
        except OSError:
            total_bytes = 0
        # Tier 1: enormous file — never materialize it (a multi-MB JSON-RPC frame
        # disconnects the host). Read a bounded byte prefix straight from disk.
        if total_bytes > disconnect_cap:
            # Byte-budgeted disconnect guard: the file is too large to decode
            # in full here (the whole point of this tier), so an exact CHAR
            # count for the original isn't available without violating the
            # never-materialize invariant -- total_bytes/disconnect_cap are
            # used as the closest cheap proxy. The recovery guidance is custom
            # (re-read this SAME file at a narrower range) rather than the
            # generic "narrow the query for full" tail: this is not a spill
            # failure, the file itself is the already-known recovery path.
            notice = f"\n\n[lc: truncated {total_bytes}→{disconnect_cap} chars; re-read narrow range=]"
            prefix_bytes = max(0, disconnect_cap - len(notice.encode("utf-8")) - 1024)
            with open(target, "rb") as fh:
                head = fh.read(prefix_bytes)
            _record_read_sig(target, exact=True)
            return {
                "mode": "full",
                "content": head.decode("utf-8", "replace") + notice,
                "path": str(target),
                "projection": SourceProjection.exact().to_dict(),
                "truncated": True,
                "bytes_total": total_bytes,
            }
        # Tier 2: moderately large — fits in memory but exceeds the host's
        # MCP-output limit, which would persist the result to a temp file and
        # force blind range re-reads. Pre-empt that with a line-aligned prefix
        # plus the EXACT continuation range, so a whole-file read costs at most a
        # couple of clean calls and the bytes are never dumped.
        if inline_budget and total_bytes > inline_budget:
            text = target.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            kept: list[str] = []
            used = 0
            for line in lines:
                line_bytes = len(line.encode("utf-8"))
                if kept and used + line_bytes > inline_budget:
                    break
                kept.append(line)
                used += line_bytes
            shown = len(kept)
            total_lines = len(lines)
            if shown < total_lines:
                notice = f'\n\n[lines 1-{shown} of {total_lines}; range="L{shown + 1}-" for rest]'
                _record_read_sig(target, exact=True)
                return {
                    "mode": "full",
                    "content": "".join(kept) + notice,
                    "path": str(target),
                    "projection": SourceProjection.exact().to_dict(),
                    "truncated": True,
                    "bytes_total": total_bytes,
                    "lines_total": total_lines,
                    "lines_shown": shown,
                }

    cap = _semantic_file_memory(_lemoncrow_root())
    try:
        payload = cap.smart_read(target, range_spec=range, expand=expand, outline_threshold=0 if outline else None)
    except FileNotFoundError as exc:
        # Append nearest basename matches (or an authoritative "no such file
        # anywhere") so the model corrects the path instead of retrying it.
        workspace_root = Path(os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()))
        suggestions = _suggest_paths_for_missing(workspace_root, target_path)
        if suggestions:
            raise FileNotFoundError(f"{exc}. Did you mean: {', '.join(suggestions)}") from exc
        raise FileNotFoundError(
            f"{exc}. No file named {Path(target_path).name!r} found under {workspace_root} — do not retry this path."
        ) from exc
    mode = payload["mode"]
    # `:outline` forces the outline_threshold above; a mode other than
    # "outline" here means the file genuinely has no outline support (plain
    # text, or too trivial for the AST/tree-sitter/generic cascade to earn its
    # savings) -- self-heal instead of silently shipping full content.
    if outline and mode != "outline":
        raise ValueError(
            f"no outline available for {target_path} (not a code file); "
            "use :summary for a gist or read the file directly"
        )
    content = payload.get("content")
    # Whitespace-minify file bodies before they enter the agent's context
    # (token optimization that works under any host/orchestrator). Only the
    # conservative transform is applied (strip trailing whitespace + collapse
    # 3+ blank-line runs), which the fuzzy edit matcher tolerates. Outline mode
    # carries no body, so it is left untouched.
    projection = SourceProjection.outline() if mode == "outline" else SourceProjection.exact()
    projection_saved = 0
    projection_delta: dict[str, Any] | None = None
    projection_result: CompactProjectionResult | MinifiedProjectionResult | None = None
    exact_read = expand or range is not None
    # M9: a non-expand 'full' body (a text/data file with no outline) over the
    # inline budget would otherwise be head+tail compacted downstream, dropping the
    # middle with no way to continue. Pre-empt with a line-aligned prefix plus an
    # EXACT continuation range (the treatment the expand path gives), and mark it
    # exact so the minify projection below leaves the line numbers intact.
    if mode == "full" and not exact_read and isinstance(content, str):
        _inline_budget = _read_inline_budget_bytes()
        if _inline_budget and len(content.encode("utf-8")) > _inline_budget:
            _src_lines = content.splitlines(keepends=True)
            _kept: list[str] = []
            _used = 0
            for _line in _src_lines:
                _lb = len(_line.encode("utf-8"))
                if _kept and _used + _lb > _inline_budget:
                    break
                _kept.append(_line)
                _used += _lb
            _shown = len(_kept)
            if _shown < len(_src_lines):
                _notice = f'\n\n[lines 1-{_shown} of {len(_src_lines)}; range="L{_shown + 1}-" for rest]'
                content = "".join(_kept) + _notice
                payload["content"] = content
                payload["truncated"] = True
                payload["lines_total"] = len(_src_lines)
                payload["lines_shown"] = _shown
                exact_read = True
    # M9b: an explicit range read carries no inline budget of its own — a
    # deliberate :L1-L10000 over a large (often LLM-generated) file would dump
    # the whole slice, which the host re-pays as cache_read every later turn.
    # Bound it like the expand/full paths: keep a line-aligned prefix and hand
    # back the EXACT continuation range, so the rest is one more call rather than
    # open-ended iteration. A normal (small) range read is untouched.
    if mode == "range" and isinstance(content, str):
        _inline_budget = _read_inline_budget_bytes()
        if _inline_budget and len(content.encode("utf-8")) > _inline_budget:
            _src_lines = content.splitlines(keepends=True)
            _kept = []
            _used = 0
            for _line in _src_lines:
                _lb = len(_line.encode("utf-8"))
                if _kept and _used + _lb > _inline_budget:
                    break
                _kept.append(_line)
                _used += _lb
            _shown = len(_kept)
            if _shown < len(_src_lines):
                _start_m = re.match(r"^L?(\d+)", str(range or ""))
                _start = int(_start_m.group(1)) if _start_m else 1
                _last = _start + _shown - 1
                _notice = f'\n\n[lines {_start}-{_last} of the requested range; range="L{_last + 1}-" for the rest]'
                content = "".join(_kept) + _notice
                payload["content"] = content
                payload["truncated"] = True
                payload["lines_total"] = len(_src_lines)
                payload["lines_shown"] = _shown
    _line_refs_trustworthy = exact_read or mode == "outline"
    if isinstance(content, str) and content and mode in ("full", "range") and not exact_read:
        from lemoncrow.pro.capabilities.prompt_compilation.tokens import count_tokens as _count_gutter_tokens
        from lemoncrow.pro.capabilities.source_projection import (
            ProjectionDelta,
            build_compact_projection,
            build_minified_projection,
            language_for_minify,
            minified_line_gutter,
        )

        language = str(payload.get("language") or "")
        # Prefer the tree-sitter minified view (comments and blank lines
        # dropped, then re-parsed); fall back to the conservative compact
        # whitespace transform when minification does not apply. Callers can
        # pin the conservative compact view via projection_kind="compact".
        force_compact = projection_kind == "compact"
        minify_lang = language_for_minify(str(target))
        if minify_lang is not None and not force_compact:
            minified = build_minified_projection(content, minify_lang, include_mapping=True, path=str(target))
            if minified.applied:
                projection_result = minified
                projection = SourceProjection.minified()
        if projection_result is None:
            compact = build_compact_projection(content, language, include_mapping=True, path=str(target))
            if compact.applied:
                projection_result = compact
                projection = SourceProjection.compact()
        if projection_result is not None:
            projection_saved = projection_result.saved_tokens
            projection_delta = ProjectionDelta(
                path=str(payload.get("path", str(target))),
                lang=language,
                original_tokens=projection_result.original_tokens,
                projected_tokens=projection_result.projected_tokens,
            ).to_dict()
            # Prefix every line with its REAL disk line number so a caller's own
            # :Lx-Ly range edit, read straight off the gutter, is already
            # disk-accurate -- no minified<->disk translation needed, and the
            # model never has to reason about which coordinate space it's in.
            if projection_result.mapping is not None:
                guttered = minified_line_gutter(projection_result.content, projection_result.mapping)
                # The "N\t" prefix on every line has its own token cost, paid
                # AFTER build_minified_projection already decided minification
                # was worthwhile on the un-guttered text -- on a repo sweep
                # (2026-07-25) that overhead alone flipped 321/414 files
                # net-negative (minify.py itself: -112 tokens vs raw). Compare
                # the FINAL guttered size against the original, not the
                # pre-gutter projected size, and only ship the gutter (and
                # trust its line numbers) when it still nets a saving.
                guttered_tokens = _count_gutter_tokens(guttered)
                if guttered_tokens < projection_result.original_tokens:
                    content = guttered
                    _line_refs_trustworthy = True
                    projection_saved = projection_result.original_tokens - guttered_tokens
                    projection_delta = ProjectionDelta(
                        path=str(payload.get("path", str(target))),
                        lang=language,
                        original_tokens=projection_result.original_tokens,
                        projected_tokens=guttered_tokens,
                    ).to_dict()
                    projection = (
                        SourceProjection.minified_with_gutter()
                        if projection.view == "minified"
                        else SourceProjection.compact_with_gutter()
                    )
                else:
                    # Gutter overhead wipes out the saving for this file -- serve
                    # the ungutted projection (still smaller than raw per the
                    # projection_saved/projection_delta set above). Line numbers
                    # are sequential projected position again, NOT disk lines,
                    # so `projection`/`_line_refs_trustworthy` stay at their
                    # pre-gutter (untrustworthy) values -- no :Lx-Ly claim.
                    content = projection_result.content
            else:
                content = projection_result.content
    elif mode == "range":
        projection = SourceProjection.range()
    # Freshness ledger: whether THIS serve's line numbers are trustworthy for a
    # later blind :Lx-Ly range edit (see _record_read_sig's exact= kwarg) -- a
    # minified/compact read counts too when it carries a disk-line gutter; a
    # bare read that neither transform actually applied to (too trivial to earn
    # savings) left `projection` at its untouched SourceProjection.exact()
    # default, which is also trustworthy even without a gutter (content is
    # verbatim disk bytes).
    _record_read_sig(resolved, exact=_line_refs_trustworthy or projection.untransformed_text)
    if mode == "full":
        _record_full_read(resolved)
    # Omit null fields: outline/range/language are absent for most reads (e.g. a
    # range read carries no outline, a plain text read no language), and a null
    # key is pure wire noise the model must skip over. Only attach them when set.
    response: dict[str, Any] = {
        "mode": mode,
        "content": content,
        "path": payload.get("path", str(target)),
        "projection": projection.to_dict(),
    }
    for _opt_key in ("outline", "range", "language", "truncated", "lines_total", "lines_shown"):
        _opt_val = payload.get(_opt_key)
        if _opt_val is not None:
            response[_opt_key] = _opt_val
    ts = int(payload.get("tokens_saved", 0) or 0) + projection_saved
    # Always carry the projection mapping when a projection replaced the body, so
    # downstream edits can map projected coordinates back to source even without
    # include_meta (the top-level mode still reads "full"; the mapping is the
    # authoritative signal that the body is transformed).
    if projection_result is not None and projection_result.mapping is not None:
        response["projection_mapping"] = projection_result.mapping.to_dict()
    # A ranged read (range=Lx-Ly / :Lx-Ly) is an exact, unprojected line
    # slice: the model asked for specific lines and already knows the range, so
    # the outline/language/projection scaffolding is pure redundancy. Return only
    # the requested lines (mode/content/path/range). Full and outline reads keep
    # their metadata; include_meta wins for power callers.
    # NOTE (follow-up): cross-turn dedup ("the model already has these exact
    # lines, return nothing") is handled generically by the dispatcher's
    # _DEDUP_TOOLS path; no read-local dedup needed here.
    if mode == "range" and not include_meta:
        response.pop("outline", None)
        response.pop("language", None)
        response.pop("projection", None)
    if include_meta:
        response["cache_hit"] = bool(payload.get("cache_hit", False))
        response["tokens_saved"] = ts
        if projection_delta is not None:
            response["projection_delta"] = projection_delta
    # Always save real savings via thread-local for the budget recorder
    if ts > 0:
        _tool_call_tokens_saved.value = ts
    return response


# Read path option parsing lives in lemoncrow_client.kit.read_path.


def _read_handler_hooks() -> ReadHandlerHooks:
    return ReadHandlerHooks(
        split_file_opts=_split_file_opts,
        op_node=_op_node,
        parse_symbol=_parse_symbol,
        smart_read_single=_smart_read_single,
        apply_batch_read_budget=_apply_batch_read_budget,
    )


configure_read_handler_hooks(_read_handler_hooks)


def _snapshot_path(raw_path: str) -> str:
    if "#cell=" in raw_path:
        return raw_path.split("#cell=", 1)[0]
    clean, _range, _full, _head, _tail, _summary, _outline = _split_file_opts(raw_path)
    match = re.search(r"#\d+(?:-\d+)?$", raw_path)
    return clean[: match.start()] if match else clean


def _resolve_snapshot_path(raw_path: str, repo_root: Path) -> tuple[str, Path]:
    """Return a ledger display path and workspace-resolved file path for snapshots."""
    clean = _snapshot_path(raw_path)
    candidate = Path(clean)
    resolved = candidate if candidate.is_absolute() else repo_root / candidate
    resolved = resolved.resolve()
    root = repo_root.resolve()
    try:
        display = str(resolved.relative_to(root))
    except ValueError:
        display = str(resolved)
    return display, resolved


def _collect_touched_paths(edits: list[dict[str, Any]], *, repo_root: str | Path | None = None) -> dict[str, Path]:
    """Extract workspace-resolved file paths referenced in edit descriptors."""
    root = Path(repo_root or Path.cwd()).resolve()
    paths: dict[str, Path] = {}
    for edit in edits:
        raw = str(edit.get("file_path") or edit.get("path") or "")
        if not raw and str(edit.get("kind") or "") == "symbol":
            from lemoncrow.pro.capabilities.tool_supervision.symbol_edit import (
                preview_symbol_edit_path,
            )

            with contextlib.suppress(Exception):
                raw = preview_symbol_edit_path(edit, repo_root=root)
        if raw:
            display, resolved = _resolve_snapshot_path(raw, root)
            paths[display] = resolved
    return dict(sorted(paths.items()))


# Files created by this process's edit tool. Tests the agent authored in this
# session are its own work in progress, not a pre-existing contract to protect.
_SESSION_CREATED_FILES: set[str] = set()


def _detect_test_weakening(snapshots: Mapping[str, FileSnapshot]) -> list[dict[str, str]]:
    """The shared test-contract guard, bound to this process's created files."""
    return _kit_detect_weakening(snapshots, session_created=_SESSION_CREATED_FILES)


def _compute_and_record_diffs(
    snapshots: Mapping[str, FileSnapshot],
) -> None:
    """Record a unified diff of each changed file to the ledger (audit/undo).

    Computed inside the edit lock so a concurrent edit can't race the post-apply
    read. Diffs are never surfaced inline to the caller: echoing old+new content
    back into context costs cache-write now and cache-read on every later turn,
    and the agent can read the file on demand if it needs to verify a change.
    """
    import difflib

    led = _get_ledger()
    # The model is only knowable from the in-flight request context, and only
    # here -- so it is read once and stamped onto every edit this call records.
    model = _request_session_model()
    for path, (fp, existed, old_content) in snapshots.items():
        try:
            new_content = fp.read_text(encoding="utf-8") if fp.exists() else None
        except Exception:
            logging.exception("Recovered from broad exception handler")
            new_content = None
        if not existed and new_content is not None:
            _SESSION_CREATED_FILES.add(str(fp.resolve()))
        if old_content == new_content:
            continue
        old_lines = (old_content or "").splitlines(keepends=True)
        new_lines = (new_content or "").splitlines(keepends=True)
        diff_text = "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))
        if diff_text:
            led.record_file_event(path=path, event="edit", diff=diff_text, model=model)
        else:
            led.record_file_event(path=path, event="edit", model=model)


def _normalize_edit_aliases(edit: dict[str, Any]) -> dict[str, Any]:
    """Promote old/new aliases (shared kit), then resolve a ``new_file`` source.

    ``new_file`` names a workspace file whose content becomes the edit's
    ``new_string``; resolving it needs this server's workspace, so it stays here.
    """
    edit = _kit_normalize_edit_aliases(edit)
    if "new_string" not in edit and isinstance(edit.get("new_file"), str) and edit["new_file"]:
        # `new` sourced from a file: huge payloads (or a preserved /tmp payload
        # from a prior failed edit) are referenced, never re-inlined -- the
        # failure mode this kills is an LLM regenerating 20KB it already wrote.
        _nf = _workspace_path(edit["new_file"])
        try:
            edit = {**edit, "new_string": _nf.read_text(encoding="utf-8", errors="replace")}
        except OSError as exc:
            raise _ToolArgumentError(
                f"new_file '{edit['new_file']}' could not be read: {exc}. "
                "new_file names a readable file whose FULL content becomes path's new "
                "content; to send content directly, pass it inline via new"
            ) from exc
        # {path: x, new_file: y} with no anchor: y's full content becomes x.
        # An explicit file reference is a deliberate whole-file source, so it
        # gets replace semantics (create or overwrite) instead of the fuzzy
        # old/new ladder or the inline-rewrite size/elision heuristics. An
        # old anchor or a :Lx-Ly path suffix still means a targeted splice.
        if (
            "old_string" not in edit
            and "replace" not in edit
            and "overwrite" not in edit
            and re.search(r":(?:L?\d+)(?:-L?\d*)?$", str(edit.get("path", "")), re.IGNORECASE) is None
        ):
            edit = {**edit, "replace": True}
    return edit


def _require_edits(edits: list[dict[str, Any]]) -> None:
    # The schema requires only `path` (so a hidden new_file retry validates
    # client-side); content presence is enforced here, after alias/new_file
    # normalization.
    try:
        _kit_require_content(edits, sources="new (or new_file)")
    except ValueError as exc:
        raise _ToolArgumentError(str(exc)) from exc


def _edit_verify_enabled(verify_flag: bool) -> bool:
    """Whether the WS1 executing edit gate should run for this call."""
    if verify_flag:
        return True
    val = os.environ.get("LEMONCROW_EDIT_VERIFY", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _apply_edit_verify_gate(
    result: dict[str, Any],
    *,
    touched: list[Path],
    snapshots: Mapping[str, FileSnapshot],
    applied_content: dict[str, str | None] | None = None,
    checks: list[str] | None,
    rollback: bool,
    timeout_ms: int,
    repo_root: Path,
) -> None:
    """Run mechanical parse/type checks; attach counterexamples and roll back on failure.

    Silent on pass: nothing is attached when the gate passes (a passing check is
    confirmation noise). Output appears only on a failure/fail-open. This gate does
    not run behavioral tests. Fully fail-open: a gate crash never blocks a
    legitimate edit.
    """
    try:
        from lemoncrow.pro.capabilities.verification.edit_gate import run_edit_gate

        checks_seq = tuple(checks) if checks else ("typecheck",)
        counterexamples = run_edit_gate(
            touched,
            repo_root=repo_root,
            checks=checks_seq,
            timeout_s=max(1.0, timeout_ms / 1000),
        )
        errors = [c for c in counterexamples if c.severity == "error"]
        # Silent on pass: a clean gate is the common case (~83% of edits) and its
        # "passed" object is pure confirmation noise. Only attach output when the
        # gate found something actionable. The rollback-on-failure behavior below
        # is unchanged -- only the success *output* is suppressed.
        if not errors:
            return
        result.setdefault("FIXME", {})["mechanical_checks"] = {
            "passed": False,
            "failures": [{k: v for k, v in c.to_dict().items() if k != "severity" and v is not None} for c in errors],
        }
        if rollback:
            # The gate runs outside the per-file edit locks (so verify can't
            # serialize concurrent edits); re-acquire them just for the rollback
            # restore-write so it can't race a concurrent edit to the same file.
            with contextlib.ExitStack() as _rb_locks:
                for _lock in _edit_path_locks(touched):
                    _rb_locks.enter_context(_lock)
                conflicts = _restore_snapshots(snapshots, applied_content)
            if conflicts:
                result["rollback_conflicts"] = conflicts
            result["rolled_back"] = True
            result["applied"] = []
            result["FIXME"]["mechanical_checks"]["rolled_back"] = True
    except Exception:
        logging.exception("Recovered from broad exception handler")
        result.setdefault("FIXME", {})["mechanical_checks"] = {
            "passed": None,
            "error": "mechanical edit gate failed open",
        }


def _contract_review_enabled() -> bool:
    """Whether post-edit contract-literal discovery runs (operator off-switch, default on)."""
    return os.environ.get("LEMONCROW_CONTRACT_REVIEW", "").strip().lower() not in ("0", "false", "no", "off")


def _loop_nudge_for_call(name: str, args: dict[str, Any]) -> str | None:
    return _canonical_loop_nudge_for_call(
        name,
        args,
        session_id_getter=lambda: _get_claude_session_id() or "",
    )


def _attach_contract_literal_review(
    result: dict[str, Any],
    edits: list[dict[str, Any]],
    *,
    repo_root: Path,
    touched_paths: list[str],
) -> None:
    """Surface remaining occurrences of contract literals this edit removed (config
    keys, wire fields, kwarg names) in files it did NOT touch.

    These parallel consumers have no call-graph edge to the edited site -- a rename
    or deletion of a quoted literal is invisible to symbol-level callers/callees, so
    the agent routinely fixes one site and misses the rest. Surfacing them while the
    edit's hypothesis is still revisable is the post-edit half of finishing a change
    at every site the contract reaches. Fail-open: a crash never affects the edit.
    """
    if not _contract_review_enabled():
        return
    try:
        from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
            contract_literal_impact,
            decorator_contract_impact,
            signature_change_impact,
            symbol_contract_impact,
        )

        engine = _code_context_engine(str(repo_root))
        impact = contract_literal_impact(
            edits,
            engine=engine,
            repo_root=repo_root,
            touched_paths=touched_paths,
        )
        sites = list(impact["sites"]) if impact else []
        # Semantic dep that literal matching misses: a removed attribute-providing
        # decorator (e.g. @lru_cache) whose methods callers still use elsewhere.
        sites.extend(decorator_contract_impact(edits, engine=engine, touched_paths=touched_paths))
        # Symbol counterpart of the literal pass: a removed/renamed module-level
        # def/class/constant whose name other files still import or reference.
        sites.extend(symbol_contract_impact(edits, engine=engine, repo_root=repo_root, touched_paths=touched_paths))
        # Signature counterpart: a def that gained a required parameter -- call sites
        # in other files that don't pass it now break.
        sites.extend(signature_change_impact(edits, engine=engine, repo_root=repo_root, touched_paths=touched_paths))
        if sites and _defer_edit_hooks():
            sites = _contract_surface_once(sites)
        if sites:
            # Merge, don't clobber: the lint-diagnostics fold may already have
            # written FIXME as {"diagnostics": [...]}. Losing error-severity
            # diagnostics to a contract-sites list would hide must-act signals.
            existing = result.get("FIXME")
            if isinstance(existing, dict):
                existing["sites"] = sites
            else:
                result["FIXME"] = sites
    except Exception:
        logging.exception("Recovered from broad exception handler")


def _applied_entry_path(entry: str | dict[str, Any]) -> str | None:
    """Extract the file path from a raw applied entry, tolerating both shapes.

    Entries can be dicts (``{"path": ...}`` / ``{"file": ...}`` / ``{"file_path": ...}``,
    as emitted by ``apply_rich_edits``) or already-compacted
    strings of the form ``"path:line,start-end"`` (as emitted by
    ``_compact_applied_entries``). A ``#10-20`` line suffix on the path is stripped so
    line-scoped edits to the same file collapse onto one path. Returns ``None`` when no
    path can be recovered.
    """
    if isinstance(entry, str):
        # Compacted form "path:spans" or plain "path"; spans only ever follow the
        # final ":" and contain digits/commas/hyphens, so split on the last ":".
        raw = entry.rsplit(":", 1)[0] if ":" in entry else entry
        raw = raw.strip()
    elif isinstance(entry, dict):
        candidate = entry.get("path") or entry.get("file") or entry.get("file_path")
        raw = str(candidate).strip() if candidate is not None else ""
    else:
        return None
    if not raw:
        return None
    # Drop a ":L10-L20" / "#cell=..." line/cell scope suffix so same-file scopes merge.
    raw = re.sub(r":L\d+(-L\d+)?$", "", raw, flags=re.IGNORECASE)
    return raw.split("#", 1)[0] or raw


def _distinct_edited_files(entries: list[Any]) -> int:
    """Count distinct files across applied entries.

    Built-in MultiEdit already batches multiple same-file hunks into one call, so
    LemonCrow's only honest advantage over a competent baseline is cross-file batching.
    Entries whose path cannot be recovered are counted as their own file so a
    legitimate cross-file edit is never under-credited.
    """
    distinct: set[str] = set()
    unparsed = 0
    for entry in entries:
        path = _applied_entry_path(entry)
        if path is None:
            unparsed += 1
        else:
            distinct.add(path)
    return len(distinct) + unparsed


# A caller-supplied edit path may carry its :Lx-Ly scope suffix; the applied
# spans are authoritative, so strip it to avoid "path:L34-L40:34-40".
_EDIT_PATH_RANGE_RE = re.compile(r":L\d+(?:-L\d+)?$")


def _compact_applied_entries(entries: list[dict[str, Any]]) -> list[str | dict[str, Any]]:
    """Group ordinary edit hunks by path; group every edit "kind" by kind too.

    Ordinary hunk edits collapse to one "path:line,line-line" string per file.
    Structural variants (notebook/projection/symbol/whole-file replace) carry a
    "kind" -- instead of one raw dict per file per kind, bucket them under a
    single {label: [...]} entry per kind: a bare path when the entry has nothing
    beyond path+kind (replace/notebook), or the remaining fields (hunks,
    match_mode, projection_kind, ...) alongside path when there's more to see.
    """
    grouped: dict[str, list[str]] = {}
    kind_groups: dict[str, list[Any]] = {}
    special: list[dict[str, Any]] = []
    _ORDINARY_KEYS = frozenset({"path", "hunks", "match_mode", "result"})
    # match_mode values that carry zero text-matching ambiguity and so compact
    # like "exact": "range" is a pure line-number splice (no old_string search
    # at all), not a match outcome to double-check. Only normalized/placeholder/
    # fuzzy -- genuine near-matches to old_string -- warrant staying a loud dict.
    _UNAMBIGUOUS_MODES = frozenset({"exact", "range"})
    # "replace" reads better as its past-tense outcome; other kinds read fine
    # as the kind name itself ("notebook", "projection", "symbol").
    _KIND_LABELS = {"replace": "replaced"}
    for entry in entries:
        kind = entry.get("kind")
        if kind:
            label = _KIND_LABELS.get(kind, kind)
            rest = {k: v for k, v in entry.items() if k not in ("path", "kind")}
            value: Any = {"path": entry.get("path", ""), **rest} if rest else str(entry.get("path", ""))
            kind_groups.setdefault(label, []).append(value)
            continue
        if set(entry) - _ORDINARY_KEYS:
            special.append(entry)
            continue
        # Non-exact fuzzy matches are actionable: keep as dicts so the model
        # sees match_mode and knows to re-read and verify the divergence.
        match_mode = entry.get("match_mode")
        if match_mode and match_mode not in _UNAMBIGUOUS_MODES:
            special.append(entry)
            continue
        path = _EDIT_PATH_RANGE_RE.sub("", str(entry.get("path", "")))
        spans = grouped.setdefault(path, [])
        for hunk in entry.get("hunks") or []:
            start = hunk.get("line_start")
            end = hunk.get("line_end")
            if isinstance(start, int) and isinstance(end, int):
                spans.append(str(start) if start == end else f"{start}-{end}")
    compact = [f"{path}:{','.join(spans)}" if spans else path for path, spans in grouped.items()]
    kind_entries = [{label: values} for label, values in kind_groups.items()]
    return [*compact, *kind_entries, *special]


# Keys whose presence makes an edit result actionable -- the model must see them.
# `calls_saved` is NOT here: it is internal savings accounting, never model-facing
# (the dispatcher pops it before rendering), so it must not keep a result "loud".
_EDIT_ACTIONABLE_KEYS = (
    # All must-act signals consolidated under FIXME: contract sites, lint
    # diagnostics, mechanical check failures. test_weakening always comes with
    # rolled_back; rollback_conflicts is the rare restore-race edge case.
    "FIXME",
    "test_weakening",
    "rollback_conflicts",
)

# Confirmation fields that are pure noise -- success is implied by the call
# returning, rollback/failure are signalled by their own keys, and atomic writes
# are all-or-nothing so the count carries no information. Stripped from EVERY
# edit result, loud or silent.
_EDIT_NOISE_KEYS = ("writes",)


def _edit_result_is_silent_success(result: dict[str, Any]) -> bool:
    """True when an edit succeeded with nothing the model must act on.

    "Applied" is implied by the call succeeding, so the common case needs no
    confirmation body. Still loud (returns False) for: a non-empty `failed`, a
    `rolled_back: true`, remaining error/warning diagnostics, a contract-literal
    review, a failed verify gate, or a fuzzy match (an `applied` entry retains
    `match_mode` only when it was NOT an exact match -- exact ones are stripped
    upstream -- so the agent is told to re-read and verify the divergence).
    """
    if result.get("rolled_back"):
        return False
    if result.get("failed"):  # only a NON-empty failed list is actionable
        return False
    for key in _EDIT_ACTIONABLE_KEYS:
        if result.get(key):
            return False
    # A non-exact (fuzzy) match survives compaction as a dict still carrying
    # `match_mode`; ordinary exact hunks compact to plain "path:line" strings.
    for entry in result.get("applied") or []:
        if isinstance(entry, dict) and entry.get("match_mode"):
            return False
    return True


def _silence_clean_edit_result(result: dict[str, Any]) -> dict[str, Any]:
    """Strip confirmation noise from every edit result; empty a clean success.

    A clean success carries NO body (the call returning IS the confirmation) --
    only `calls_saved` is kept, which the dispatcher reads for savings accounting
    and strips before rendering, so it never reaches the model. A LOUD result
    (failure, rollback, diagnostics, review, failed gate, fuzzy match) keeps its
    actionable content but still sheds the noise fields: `writes` always, an
    empty `failed`, and a false `rolled_back`.
    """
    if _edit_result_is_silent_success(result):
        # Keep a MINIMAL `applied` range echo (path:line, not a diff) so the model
        # stays oriented and does not re-read the file it just edited. Everything
        # else (writes/hooks/empty-failed) is still dropped.
        silent: dict[str, Any] = {}
        applied = result.get("applied")
        # Only echo the compact "path:line" strings (exact edits). Symbol/special
        # edits keep dict entries -- leave those silent rather than dump raw hunks.
        if applied and all(isinstance(a, str) for a in applied):
            silent["applied"] = applied
        if "calls_saved" in result:
            silent["calls_saved"] = result["calls_saved"]
        # A redirected edit is never silent about being redirected: the whole
        # point of the disclosure is that it survives the clean-success path,
        # which is exactly where a wrong worktree inference would hide.
        if "resolved_against" in result:
            silent["resolved_against"] = result["resolved_against"]
        return silent
    for key in _EDIT_NOISE_KEYS:
        result.pop(key, None)
    if result.get("failed") == []:
        result.pop("failed", None)
    if result.get("rolled_back") is False:
        result.pop("rolled_back", None)
    return result


def _reindex_edited_files(repo_root: Path, touched_paths: list[str]) -> None:
    """Immediately refresh the shared code index for files this edit touched.

    Keeps ``search``/``explore`` consistent with the just-applied edit instead of
    waiting for the engine's autosync poll (~10s). Incremental: re-extracts only
    the touched files (O(edited files)), never a full rebuild, and runs against
    the long-lived, process-shared engine so the next code tool call sees the
    change.

    Runs OFF the edit hot path on a daemon thread: a warm per-file reindex still
    costs hundreds of ms, and the edit response must not block on it. The model's
    next tool call is a network+thinking round-trip away, so the refresh lands
    first in practice; the autosync poll is the backstop if it doesn't. Fail-open;
    off-switch LEMONCROW_EDIT_REINDEX=0.
    """
    if not touched_paths:
        return
    if os.environ.get("LEMONCROW_EDIT_REINDEX", "").strip().lower() in ("0", "false", "no", "off"):
        return

    def _run() -> None:
        try:
            _code_context_engine(str(repo_root))._reindex_files(touched_paths)
        except Exception:
            logging.exception("Recovered from broad exception handler")

    threading.Thread(target=_run, name="lemoncrow-edit-reindex", daemon=True).start()


# Max lint/type diagnostics folded into an edit result's FIXME block; the rest
# collapse to a "+K more" line (they are one linter run away, not lost).
#
# Kept deliberately small. This block is appended to EVERY edit result, so it is
# re-billed on every later round-trip of the session -- a 20-line lint dump after
# each of 8 edits is a wall of text the agent pays for repeatedly. A handful is
# enough to signal "this file has findings, act on them"; the tail is one linter
# run away.
_EDIT_DIAG_CAP = 5
# Same rationale as _EDIT_DIAG_CAP: a dirty tree with a long status listing
# must not dump an unbounded VCS report into the edit result.
_EDIT_VCS_CAP = 10

# Retry anchors exist to let the agent re-issue a rejected range edit with an
# `old=` that matches disk -- they are NOT a content channel. Unbounded, a batch
# of rejected edits echoes every target region back verbatim: one measured Cursor
# run returned an 11,433-char rejection payload (28x the 204-char native edit
# result) because four failed edits each carried their full region. A whole-line
# prefix is still an exact substring of the region, so a bounded anchor stays
# directly usable as `old=`.
_RETRY_ANCHOR_CHARS = 400


def _anchor_snippet(text: str, limit: int = _RETRY_ANCHOR_CHARS) -> str:
    """Bound a retry anchor to whole leading lines within ``limit`` chars.

    Truncating on a line boundary keeps the result a valid substring of the
    region, so passing it straight back as ``old=`` still anchors the retry.
    """
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        if kept and used + len(line) > limit:
            break
        kept.append(line)
        used += len(line)
    return "".join(kept) if kept else text[:limit]


def _edit_handler_hooks() -> EditHandlerHooks:
    return EditHandlerHooks(
        anchor_snippet=_anchor_snippet,
        apply_edit_verify_gate=_apply_edit_verify_gate,
        attach_contract_literal_review=_attach_contract_literal_review,
        claude_additional_dirs=_claude_additional_dirs,
        code_context_engine=_code_context_engine,
        collect_touched_paths=_collect_touched_paths,
        compact_applied_entries=_compact_applied_entries,
        compute_and_record_diffs=_compute_and_record_diffs,
        detect_test_weakening=_detect_test_weakening,
        distinct_edited_files=_distinct_edited_files,
        edit_path_locks=_edit_path_locks,
        edit_verify_enabled=_edit_verify_enabled,
        is_within_root=_is_within_root,
        line_digests=_line_digests,
        normalize_edit_aliases=_normalize_edit_aliases,
        range_read_sigs=_range_read_sigs,
        reindex_edited_files=_reindex_edited_files,
        relocate_served_range=_relocate_served_range,
        require_edits=_require_edits,
        restore_snapshots=_restore_snapshots,
        retarget_range_edit=_retarget_range_edit,
        session_worktree_root=_session_worktree_root,
        silence_clean_edit_result=_silence_clean_edit_result,
        snapshot_paths=_snapshot_paths,
        test_contract_guard_enabled=_test_contract_guard_enabled,
        workspace_path=_workspace_path,
        workspace_root=_workspace_root,
        edit_diag_cap=_EDIT_DIAG_CAP,
        edit_vcs_cap=_EDIT_VCS_CAP,
    )


configure_edit_handler_hooks(_edit_handler_hooks)


# SQL tool registration lives in mcp.tools_utility.


_TASK_BOUNDARY_SUCCESS_RE = re.compile(
    r"\b(done|complete|completed|success|successful|passed|tests?\s+pass(?:ed)?|validated|verified|committed|lgtm)\b",
    re.IGNORECASE,
)
_TASK_BOUNDARY_FAILURE_RE = re.compile(
    r"\b(fail(?:ed|ure)?|error|exception|traceback|blocked|todo|not\s+done|not\s+complete)\b",
    re.IGNORECASE,
)


def _ledger_turn_count(led: RunLedger) -> int:
    turn_events = [
        event
        for event in led.events
        if event.kind in {"agent_message", "reasoning", "test_result", "command_result", "tool_result"}
    ]
    if turn_events:
        return len(turn_events)
    return len(led.events)


def _event_text(event: Any) -> str:
    summary = str(getattr(event, "summary", ""))
    payload = getattr(event, "payload", {})
    return f"{summary}\n{json.dumps(payload, ensure_ascii=False, default=str)}"


def _task_boundary_detected(led: RunLedger) -> bool:
    """Return true only when recent ledger events show a clean stopping point."""
    for event in led.events[-3:]:
        text = _event_text(event)
        if _TASK_BOUNDARY_SUCCESS_RE.search(text) and not _TASK_BOUNDARY_FAILURE_RE.search(text):
            if event.kind == "test_result":
                return bool(event.payload.get("passed"))
            if event.kind == "command_result":
                return bool(event.payload.get("ok"))
            # A SUCCESS keyword in free-text (agent_message / reasoning / note)
            # is the agent *talking* about completion, not a structured outcome,
            # so it must NOT count as a boundary (it would auto-compact mid-task).
    return False


def _context_lifecycle_decision(led: RunLedger) -> dict[str, Any]:
    tokens_used = led.token_count
    utilisation_pct = round(100.0 * tokens_used / CONTEXT_WINDOW_TOKENS, 1)
    turn_count = _ledger_turn_count(led)
    boundary = _task_boundary_detected(led)
    should_handover = utilisation_pct >= HANDOVER_THRESHOLD
    # Bypass the min-turns gate when utilisation is already very high - a small
    # number of dense turns (huge tool outputs, large file reads) can fill the
    # window just as fast as many small ones.
    turns_gate_passed = turn_count > AUTO_COMPACT_MIN_TURNS or utilisation_pct >= AUTO_COMPACT_HIGH_UTIL_OVERRIDE
    should_auto_compact = (
        not should_handover and utilisation_pct >= AUTO_COMPACT_THRESHOLD and turns_gate_passed and boundary
    )
    should_advise = utilisation_pct >= COMPACT_ADVISORY_THRESHOLD

    if should_handover:
        reason = "context utilization reached handover threshold"
    elif should_auto_compact:
        reason = "context utilization reached auto-compact threshold at a task boundary"
    elif utilisation_pct >= AUTO_COMPACT_THRESHOLD and not turns_gate_passed:
        reason = f"auto-compact gated: fewer than {AUTO_COMPACT_MIN_TURNS} turns and below {AUTO_COMPACT_HIGH_UTIL_OVERRIDE}% override"
    elif utilisation_pct >= AUTO_COMPACT_THRESHOLD and not boundary:
        reason = "auto-compact waiting for a clean task boundary"
    elif should_advise:
        reason = "advisory threshold reached; no automatic action"
    else:
        reason = "below advisory threshold"

    return {
        "tokens_used": tokens_used,
        "context_window": CONTEXT_WINDOW_TOKENS,
        "utilisation_pct": utilisation_pct,
        "turn_count": turn_count,
        "task_boundary_detected": boundary,
        "should_advise": should_advise,
        "should_auto_compact": should_auto_compact,
        "should_compact": should_auto_compact,
        "should_handover": should_handover,
        "reason": reason,
        "thresholds": {
            "advisory_pct": COMPACT_ADVISORY_THRESHOLD,
            "auto_compact_pct": AUTO_COMPACT_THRESHOLD,
            "handover_pct": HANDOVER_THRESHOLD,
            "auto_compact_min_turns": AUTO_COMPACT_MIN_TURNS,
        },
    }


def _write_handover_packet(led: RunLedger, state: Any) -> Path:
    from lemoncrow.core.foundation.paths import session_dir
    from lemoncrow.pro.runtime.context_compressor import HandoverPacket

    root = _lemoncrow_root()
    run_dir = session_dir(root, led.agent or _detect_agent(), led.session_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    handover_path = run_dir / "HANDOVER.md"
    packet = HandoverPacket.from_ledger(led, state, workspace_root=_workspace_root())
    handover_path.write_text(packet.to_markdown(), encoding="utf-8")
    return handover_path


_COMPACT_ADVISE_CACHE: dict[str, tuple[int, Any]] = {}
_MAX_COMPACT_ADVISE_CACHE = 64
# Serializes cache read/insert/evict under the dispatcher thread pool; the
# expensive compress() call runs outside the lock.
_COMPACT_ADVISE_LOCK = threading.Lock()


def _compact_advise(session_id: str | None = None) -> dict[str, Any]:
    """Advise when to compact and what context to preserve.

    Returns a manifest with:
    - should_advise: bool (true if utilisation >= 60%)
    - should_compact: bool (true if utilisation >= 80%, after min-turn and boundary gates)
    - should_handover: bool (true if utilisation >= 95%)
    """
    try:
        from lemoncrow.pro.runtime.context_compressor import ContextCompressor

        led = _get_ledger()
        if session_id:
            led.session_id = session_id

        lifecycle = _context_lifecycle_decision(led)
        utilisation_pct = float(lifecycle["utilisation_pct"])
        should_compact = bool(lifecycle["should_compact"])
        should_handover = bool(lifecycle["should_handover"])
        # Memoize the full-ledger compression by (session, event count): this
        # advisory entrypoint may be polled repeatedly without the ledger
        # changing, and compress() walks every event each call. compress() is
        # pure (reads events, returns a fresh CompactState), so reuse is safe.
        _ca_key = led.session_id or ""
        with _COMPACT_ADVISE_LOCK:
            _ca_cached = _COMPACT_ADVISE_CACHE.get(_ca_key)
        if _ca_cached is not None and _ca_cached[0] == len(led.events):
            state = _ca_cached[1]
        else:
            state = ContextCompressor().compress(led, preserve_last_n_turns=10, workspace_root=_workspace_root())
            with _COMPACT_ADVISE_LOCK:
                _COMPACT_ADVISE_CACHE[_ca_key] = (len(led.events), state)
                if len(_COMPACT_ADVISE_CACHE) > _MAX_COMPACT_ADVISE_CACHE:
                    # Bound the cache: a marathon process seeing many session ids
                    # must not leak compressed states. Evict oldest (skip current).
                    for _stale in list(_COMPACT_ADVISE_CACHE)[: len(_COMPACT_ADVISE_CACHE) - _MAX_COMPACT_ADVISE_CACHE]:
                        if _stale != _ca_key:
                            _COMPACT_ADVISE_CACHE.pop(_stale, None)
        compaction_savings = _session_compaction_savings_payload(
            led,
            state,
            tokens_before=int(lifecycle["tokens_used"]),
            trigger="compact_advise",
            reason=str(lifecycle["reason"]),
            utilisation_pct=utilisation_pct,
        )

        # Collect preserve_playbooks: top active Playbooks from ledger
        preserve_playbooks = list(set(led.active_playbooks))[:3]

        # Collect pin_memory: pinned MemoryBlocks for this run's agent
        pin_memory: list[str] = []
        try:
            store = _memory_store()
            agent_id = led.agent or "claude"
            pinned = store.list_pinned_blocks(agent_id=agent_id)
            pin_memory = [b.id for b in pinned][:5]
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.warning(
                "Suppressed exception in _compact_advise fetching pinned memory",
                exc_info=True,
            )

        # Collect open_files: last 5 files touched
        open_files = led.files_touched[-5:] if led.files_touched else []
        handover_file: str | None = None
        if should_handover:
            handover_file = str(_write_handover_packet(led, state))

        # Build suggested prompt
        if should_handover:
            suggested_prompt = (
                f"Session is at {utilisation_pct}% context utilisation. Read {handover_file} and continue "
                "from a fresh agent context using the host-native agent/subagent mechanism."
            )
        else:
            suggested_prompt = (
                f"Compact this conversation. Context utilisation: {utilisation_pct}%. "
                f"Please preserve these Playbooks: {', '.join(preserve_playbooks) or '(none yet)'}. "
                f"Recently edited files: {', '.join(open_files) or '(none)'}. "
                "Preserve the last 10 raw turns, active errors, and current CLAUDE.md hash."
            )

        # Persist manifest to disk
        try:
            from lemoncrow.core.foundation.paths import session_dir

            root = _lemoncrow_root()
            run_dir = session_dir(root, led.agent or _detect_agent(), led.session_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = run_dir / "compact_manifest.json"
            manifest = {
                "created_at": datetime.now(UTC).isoformat(),
                "session_id": led.session_id,
                "should_compact": should_compact,
                "should_advise": bool(lifecycle["should_advise"]),
                "should_auto_compact": bool(lifecycle["should_auto_compact"]),
                "should_handover": should_handover,
                "utilisation_pct": utilisation_pct,
                "turn_count": int(lifecycle["turn_count"]),
                "task_boundary_detected": bool(lifecycle["task_boundary_detected"]),
                "reason": str(lifecycle["reason"]),
                "thresholds": lifecycle["thresholds"],
                "preserve_playbooks": preserve_playbooks,
                "pin_memory": pin_memory,
                "open_files": open_files,
                "recent_turns": state.recent_turns,
                "claude_md_hash": state.claude_md_hash,
                "active_errors": state.error_fingerprints,
                "handover_file": handover_file,
                "suggested_prompt": suggested_prompt,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.warning(
                "Suppressed exception in _compact_advise persisting manifest",
                exc_info=True,
            )

        if should_compact and int(compaction_savings["tokens_saved"]) > 0:
            _append_live_savings_event(compaction_savings)

        return {
            "should_compact": should_compact,
            "should_advise": bool(lifecycle["should_advise"]),
            "should_auto_compact": bool(lifecycle["should_auto_compact"]),
            "should_handover": should_handover,
            "utilisation_pct": utilisation_pct,
            "turn_count": int(lifecycle["turn_count"]),
            "task_boundary_detected": bool(lifecycle["task_boundary_detected"]),
            "reason": str(lifecycle["reason"]),
            "thresholds": lifecycle["thresholds"],
            "preserve_playbooks": preserve_playbooks,
            "pin_memory": pin_memory,
            "open_files": open_files,
            "recent_turns": state.recent_turns,
            "claude_md_hash": state.claude_md_hash,
            "active_errors": state.error_fingerprints,
            "handover_file": handover_file,
            "suggested_prompt": suggested_prompt,
            "tokens_before": int(compaction_savings["tokens_before"]),
            "tokens_after_estimate": int(compaction_savings["tokens_after_estimate"]),
            "tokens_freed": int(compaction_savings["tokens_freed"]),
            "cost_saved_usd": float(compaction_savings["cost_saved_usd"]),
        }
    except Exception:
        logging.exception("Recovered from broad exception handler")
        # Fail-open: return conservative defaults
        return {
            "should_compact": False,
            "should_advise": False,
            "should_auto_compact": False,
            "should_handover": False,
            "utilisation_pct": 0.0,
            "turn_count": 0,
            "task_boundary_detected": False,
            "reason": "Unable to compute compaction advice; proceed conservatively.",
            "thresholds": {
                "advisory_pct": COMPACT_ADVISORY_THRESHOLD,
                "auto_compact_pct": AUTO_COMPACT_THRESHOLD,
                "handover_pct": HANDOVER_THRESHOLD,
                "auto_compact_min_turns": AUTO_COMPACT_MIN_TURNS,
            },
            "preserve_playbooks": [],
            "pin_memory": [],
            "open_files": [],
            "recent_turns": [],
            "claude_md_hash": None,
            "active_errors": [],
            "handover_file": None,
            "suggested_prompt": "Unable to compute compaction advice; proceed with default compaction.",
        }


def _memory_summary(session_id: str) -> dict[str, Any]:
    """Run the sleeptime summarizer for a given run and return a summary.

    Input:
        session_id: The run identifier to summarize.

    Output:
        tokens_pre, tokens_post, summary_md, evicted_event_ids, strategy
    """
    try:
        from lemoncrow.pro.capabilities.context_compression.capability import (
            ContextCompressionCapability,
        )

        led = _get_ledger()
        if session_id:
            led.session_id = session_id

        cap = ContextCompressionCapability()
        result = cap.compress_with_sleeptime(led)

        summary_lines = [f"## Sleeptime Summary - run `{led.session_id}`", ""]
        summary_lines.append(f"- Tokens before: {result.chars_before // 4}")
        summary_lines.append(f"- Tokens after:  {result.chars_after // 4}")
        summary_lines.append(f"- Reduction:     {result.reduction_pct}%")
        if result.dropped:
            summary_lines.append("")
            summary_lines.append("### Evicted events")
            for d in result.dropped[:10]:
                summary_lines.append(f"- [{d.kind}] {d.summary[:100]}")

        return {
            "tokens_pre": result.chars_before // 4,
            "tokens_post": result.chars_after // 4,
            "summary_md": "\n".join(summary_lines),
            "evicted_event_ids": [d.kind for d in result.dropped],
            "strategy": "tfidf",
        }
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        return {"error": str(exc)}


def _code_engine_cache_limit() -> int:
    return _canonical_code_engine_cache_limit()


def _code_context_engine(repo_root: str = ".") -> Any:
    return _canonical_code_context_engine(repo_root, workspace=_workspace_root())


def _scoped_context_capability(repo_root: str = ".") -> Any:
    return _canonical_scoped_context_capability(
        repo_root,
        workspace=_workspace_root(),
        engine_factory=lambda target: _code_context_engine(target),
    )


def _workspace_code_router(repo_root: str = ".") -> Any:
    return _canonical_workspace_code_router(
        repo_root,
        workspace=_workspace_root(),
        engine_factory=lambda target: _code_context_engine(target),
    )


# Fields that are purely internal LemonCrow bookkeeping — never useful to an LLM.
# Keep: repo_name (multi-repo).
_CODE_OP_TOP_STRIP: frozenset[str] = frozenset(
    {
        "symbol_id",
        "cache_hit",
        "rendered_format",
        "repo_id",
        "total_tokens",
        "tokens_saved",
        "provenance",
        "provenance_breakdown",
        "mode",
        "view",
        "has_more_context",
        "suggested_next",
        "explanation",
        "text_search",
    }
)

# Fields to strip from nested item dicts (search results, callers/related lists, etc.).
# Keep: origin (external/internal scope), repo_name (multi-repo workspace).
_CODE_OP_ITEM_STRIP: frozenset[str] = frozenset(
    {
        "symbol_id",
        "start_byte",
        "end_byte",
        "content_hash",
        "repo_id",
        "score",
        "provenance",
    }
)

# Extra top-level keys to drop per-op (in addition to _CODE_OP_TOP_STRIP).
_CODE_OP_EXTRA_STRIP: dict[str, frozenset[str]] = {
    # edges contain only hash IDs — no names or paths; `related` has the useful data
    "callers": frozenset({"edges"}),
    "callees": frozenset({"edges"}),
    # symbol/node ops: byte offsets and hashes are useless to LLMs (same payload
    # shape — both come from engine.tool_symbol). node additionally renders the
    # source body; symbol stays a compact location/signature summary.
    "symbol": frozenset({"start_byte", "end_byte", "content_hash", "score"}),
    "node": frozenset({"start_byte", "end_byte", "content_hash", "score"}),
    # search: `snippet` at top level is just the mode string ("none"/"head"/"full"), not actual code
    "search": frozenset({"snippet"}),
    # context: `symbols` duplicates entry_points with heavy metadata; telemetry/import_neighbors are internal
    "context": frozenset({"telemetry", "import_neighbors", "symbols"}),
    # status: db_path exposes internal filesystem paths
    "status": frozenset({"db_path"}),
}

# List-valued fields whose items should be stripped of internal keys.
_CODE_OP_ITEM_LIST_FIELDS: tuple[str, ...] = (
    "items",
    "related",
    "related_symbols",
    "entry_points",
    "references",
    "symbols",
)


def _strip_code_item(item: dict[str, Any]) -> dict[str, Any]:
    """Strip internal bookkeeping from a single result item."""
    cleaned = {k: v for k, v in item.items() if k not in _CODE_OP_ITEM_STRIP}
    if cleaned.get("origin") == "internal":
        del cleaned["origin"]
    if cleaned.get("qualified_name") and cleaned["qualified_name"] == (
        cleaned.get("name") or cleaned.get("symbol_name")
    ):
        del cleaned["qualified_name"]
    cleaned.pop("role", None)
    return cleaned


def _strip_code_op_response(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Remove internal/telemetry fields that waste LLM context."""
    drop = _CODE_OP_TOP_STRIP | _CODE_OP_EXTRA_STRIP.get(op, frozenset())
    result: dict[str, Any] = {k: v for k, v in payload.items() if k not in drop}

    # Save real tokens_saved via thread-local so _record_context_budget_for_tool
    # can read it without polluting the LLM-facing response.
    ts = int(payload.get("tokens_saved", 0) or 0)
    if ts > 0:
        _tool_call_tokens_saved.value = ts

    # Strip internal keys from the target object
    if isinstance(result.get("target"), dict):
        result["target"] = _strip_code_item(result["target"])

    # Strip internal keys from list fields (or dicts of lists, e.g. references grouped by file)
    for field in _CODE_OP_ITEM_LIST_FIELDS:
        value = result.get(field)
        if isinstance(value, list):
            result[field] = [_strip_code_item(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            result[field] = {
                key: (
                    [_strip_code_item(item) if isinstance(item, dict) else item for item in group]
                    if isinstance(group, list)
                    else group
                )
                for key, group in value.items()
            }

    return result


def _maybe_attach_code_rendered(op: str, payload: dict[str, Any], *, render_compact: bool) -> dict[str, Any]:
    # Render first so the markdown uses all original fields (e.g. repo_id for cache_status heading).
    from lemoncrow.pro.capabilities.code_context.renderer import render_code_payload

    rendered = render_code_payload(op, payload)

    # Store in thread-local so _handle can use MD text as the MCP response body.
    # This is the single model-facing channel for the rendered markdown: for
    # code-intel tools render_tool_result_text() returns this value as the MCP
    # response body. Do NOT also stash it in result["rendered"] — that in-JSON
    # copy would ship the same markdown twice to the model.
    _tool_call_rendered_text.value = rendered
    _ = render_compact  # rendered text now travels only via the response body

    # Strip internal fields after rendering — LLMs get clean JSON without duplicating
    # internal bookkeeping that only LemonCrow needs.
    result = _strip_code_op_response(op, payload)

    # Inject cold-start bootstrap note so the LLM knows results may be incomplete.
    if op not in {"index", "status", "cache_status"}:
        engine = getattr(_code_engine_for_current_call, "value", None)
        if engine is not None and not Path(engine.db_path).exists():
            result["bootstrap_note"] = (
                "Repository not yet indexed — results may be incomplete. "
                "Run `lc code index` (or `lc project init`) to bootstrap the index."
            )

    return result


def _code_search_target_item(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": item.get("kind"),
        "name": item.get("name") or item.get("symbol_name"),
        "qualified_name": item.get("qualified_name"),
        "path": item.get("path") or item.get("file_path"),
        "repo_name": item.get("repo_name"),
        "project_id": item.get("project_id"),
        "origin": item.get("origin"),
        "line": item.get("line") or item.get("start_line"),
        "end_line": item.get("end_line"),
        "signature": item.get("signature"),
        "snippet": item.get("snippet"),
        "deleted_at": item.get("deleted_at"),
        "deleted_at_sha": item.get("deleted_at_sha"),
        "rename_target": item.get("rename_target"),
        "rename_note": item.get("rename_note"),
    }
    return {key: value for key, value in result.items() if value is not None}


def _code_search_target_view(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    if isinstance(items, list):
        # Copy before mutating so a cached/shared engine payload is never
        # rewritten in place under concurrent callers.
        payload = dict(payload)
        payload["items"] = [_code_search_target_item(item) if isinstance(item, dict) else item for item in items]
    return payload


def _flatten_code_references(references: Any) -> list[dict[str, Any]]:
    if isinstance(references, list):
        return [dict(item) for item in references if isinstance(item, dict)]
    if isinstance(references, dict):
        flattened: list[dict[str, Any]] = []
        for values in references.values():
            if isinstance(values, list):
                flattened.extend(dict(item) for item in values if isinstance(item, dict))
        return flattened
    return []


def _code_search_graph_view(
    engine: Any,
    *,
    query: str,
    search_payload: dict[str, Any],
    view: Literal["graph", "explain"],
    limit: int,
    depth: int,
    budget_tokens: int,
) -> dict[str, Any]:
    items = search_payload.get("items")
    primary = next((item for item in items if isinstance(item, dict)), None) if isinstance(items, list) else None
    if primary is None:
        return {
            "target": None,
            "related": {"imports": [], "usages": [], "callers": [], "callees": []},
        }

    target = _code_search_target_item(primary)
    symbol_args = {
        "query": query,
        "symbol_id": primary.get("symbol_id") or primary.get("id"),
        "qualified_name": primary.get("qualified_name"),
        "symbol_name": primary.get("symbol_name") or primary.get("name"),
        "file_path": primary.get("file_path") or primary.get("path"),
    }
    relation_budget = max(300, budget_tokens // 3)
    usages = engine.tool_usages(
        query=symbol_args["query"],
        symbol_id=symbol_args["symbol_id"],
        qualified_name=symbol_args["qualified_name"],
        symbol_name=symbol_args["symbol_name"],
        file_path=symbol_args["file_path"],
        group_by="none",
        snippet_lines=0,
        limit=limit,
        budget_tokens=relation_budget,
        auto_index=False,
    )
    callers = engine.tool_callers(
        query=symbol_args["query"],
        symbol_id=symbol_args["symbol_id"],
        qualified_name=symbol_args["qualified_name"],
        symbol_name=symbol_args["symbol_name"],
        file_path=symbol_args["file_path"],
        depth=depth,
        limit=limit,
        budget_tokens=relation_budget,
        auto_index=False,
    )
    callees = engine.tool_callees(
        query=symbol_args["query"],
        symbol_id=symbol_args["symbol_id"],
        qualified_name=symbol_args["qualified_name"],
        symbol_name=symbol_args["symbol_name"],
        file_path=symbol_args["file_path"],
        depth=depth,
        limit=limit,
        budget_tokens=relation_budget,
        auto_index=False,
    )
    refs = _flatten_code_references(usages.get("references"))
    imports = [ref for ref in refs if "import" in str(ref.get("edge_kind") or "")]
    import_ids = {id(ref) for ref in imports}
    usage_refs = [ref for ref in refs if id(ref) not in import_ids]
    payload: dict[str, Any] = {
        "target": target,
        "related": {
            "imports": imports,
            "usages": usage_refs,
            "callers": callers.get("related", []),
            "callees": callees.get("related", []),
        },
    }
    if view == "explain":
        payload["items"] = [
            _code_search_target_item(item) if isinstance(item, dict) else item
            for item in cast(list[Any], search_payload.get("items", []))
        ]
    return payload


# Result keys that represent batched discoveries — each item would have
# required its own naive grep/read in a side-by-side baseline.
_CODE_BATCH_KEYS: tuple[str, ...] = (
    "matches",
    "callers",
    "callees",
    "usages",
    "related",
    "edges",
    "references",
    "results",
    "items",
    "files",
    "symbols",
    "routes",
)


# Vanilla Read dumps up to 2000 lines per file (~40 chars/line of typical
# source). Counterfactual reads of surfaced files are capped here per file so
# a giant file can never inflate the credit past what vanilla would inline.
_VANILLA_READ_FILE_CAP_CHARS = 80_000


def _finish_code_result(result: dict[str, Any]) -> dict[str, Any]:
    # Infer calls_saved. One code-intel call replaces a locating grep plus a
    # read per DISTINCT file surfaced -- not one call per returned item (20
    # symbols across 3 files is ~3 avoided reads, not 19 avoided calls).
    # Credit distinct files: (1 grep + N reads) - the 1 call made = N; that
    # holds at N=1 too (grep + read - this call = 1), and single-file results
    # are the common code_search shape, so they must not fall through
    # uncredited. Items without file info credit only the locate scan.
    surfaced_paths: set[str] = set()
    if isinstance(result, dict):
        for key in _CODE_BATCH_KEYS:
            items = result.get(key)
            if isinstance(items, list) and items:
                # Graveyard hits (deleted_at) describe files that no longer
                # exist — vanilla had no grep+read to avoid, so they earn no
                # call or token credit and keep the deleted surface additive.
                live_items = [item for item in items if isinstance(item, dict) and not item.get("deleted_at")]
                if not live_items and any(isinstance(item, dict) for item in items):
                    break
                paths = {str(item.get("path") or item.get("file") or "") for item in live_items}
                paths.discard("")
                surfaced_paths = paths
                if "calls_saved" not in result:
                    result["calls_saved"] = len(paths) if paths else 1
                break
    # Counterfactual token credit: vanilla explores by grepping and then
    # Reading each surfaced file; what it would have inlined (capped per file)
    # minus what this one call actually returned is context we kept out.
    # The engine may already stamp a packing credit (projected vs full symbol
    # source) -- both measure the same avoidance against different baselines,
    # so take the LARGER of the two, never the sum.
    if surfaced_paths and isinstance(result, dict):
        per_file_chars: dict[str, int] = {}
        for p in surfaced_paths:
            try:
                per_file_chars[p] = min(os.stat(p).st_size, _VANILLA_READ_FILE_CAP_CHARS)
            except OSError:
                continue  # non-file / unreadable path: no counterfactual read
        vanilla_chars = sum(per_file_chars.values())
        if vanilla_chars > 0:
            try:
                returned_chars = len(json.dumps(result, default=str))
            except (TypeError, ValueError):
                returned_chars = 0
            counterfactual = max(0, vanilla_chars - returned_chars) // 4
            existing = max(
                _coerce_saved_tokens(result.get("tokens_saved")),
                int(getattr(_tool_call_tokens_saved, "value", 0) or 0),
            )
            if counterfactual > existing:
                result["tokens_saved"] = counterfactual
                # Hand the per-file breakdown to _process_tool_accounting so
                # the credit is netted against the shared per-session
                # credited-file set (one content-avoidance credit per file).
                _tool_call_counterfactual.value = {
                    "per_file_chars": per_file_chars,
                    "returned_chars": returned_chars,
                    "floor_tokens": existing,
                }
    engine = getattr(_code_engine_for_current_call, "value", None)
    if engine is not None and isinstance(result, dict) and "index_status" not in result:
        try:
            if not engine.index_ready():
                result["index_status"] = "warming"
                result.setdefault(
                    "hint",
                    "code index is still building in the background; retry shortly for complete results",
                )
        except Exception:
            logging.exception("Recovered from broad exception handler")
    return result


def _code_engine_at(repo_root: str | None) -> Any:
    engine = _code_context_engine(repo_root or ".")
    _code_engine_for_current_call.value = engine
    return engine


_GRAPH_KINDS: frozenset[str] = frozenset(
    {
        "blast_radius",
        "dead_code",
        "cycles",
        "coupling",
        "centrality",
        # WS10 code health & history (G15/G16/N17): additive, read-only, fail-open.
        "design_gaps",
        "verify_design",
        "pr_risk",
        "commit_provenance",
        "index_docs",
        "recall_docs",
        # WS11 (G17): module-boundary / god-module topology discovery.
        "topology",
    }
)


def _synthesize_edges_for_paths(paths: list[str]) -> list[dict[str, Any]]:
    """Run the bounded N2/N3 edge synthesizer over *paths*; clearly-labelled output.

    Returns a flat list of heuristic edge dicts (provenance="heuristic"). Never
    touches the static call graph -- this is a separate, opt-in addendum.
    """
    from lemoncrow.infra.code_intel.languages import language_for_path
    from lemoncrow.pro.capabilities.code_context.edge_synthesis import synthesize_edges

    out: list[dict[str, Any]] = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_file():
            continue
        lang = language_for_path(candidate)
        language = lang.name if lang is not None else ""
        source = candidate.read_text(encoding="utf-8", errors="replace")
        for edge in synthesize_edges(source, language=language):
            out.append({"file": str(candidate), **edge.to_dict()})
    return out


def _op_graph(
    *,
    kind: str = "blast_radius",
    path: str | None = None,
    paths: list[str] | None = None,
    limit: int = 50,
    synthesize: bool = False,
    query: str | None = None,
    enable: bool | None = None,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    """Agent-facing graph analytics (G3/G6) dispatched by ``kind``.

    * ``blast_radius`` (default) -- reverse-dependency closure + affected tests +
      risk tier for ``path`` (file-level; uses the existing change_impact).
    * ``dead_code`` / ``cycles`` / ``coupling`` -- repo-wide file-graph analytics
      over the semantic file index. Pass ``paths`` to fold those files into the
      index first; otherwise analyses whatever the index already holds.
    * ``centrality`` -- symbol-level call-graph centrality from the code-intel engine.
      Pass ``synthesize=true`` with ``paths`` to additionally return
      heuristic (route/event) edges as a SEPARATE ``synthesized_edges`` list
      (N2/N3); they are never merged into the static call graph.
    """
    if kind not in _GRAPH_KINDS:
        raise ValueError(f"unknown graph kind: {kind!r}; expected one of {sorted(_GRAPH_KINDS)}")

    # WS10 code health & history kinds (G15/G16/N17). Each is additive, read-only
    # analytics that fails open inside its own module; dispatched before the
    # file-graph kinds because they have distinct argument shapes.
    if kind in {"design_gaps", "verify_design", "pr_risk", "commit_provenance", "index_docs", "recall_docs"}:
        return _op_graph_code_health(
            kind=kind, path=path, paths=paths, limit=limit, query=query, enable=enable, repo_root=repo_root
        )

    if kind == "centrality":
        engine = _code_engine_at(repo_root)
        result = cast(dict[str, Any], engine.call_graph_centrality(limit=limit))
        result["kind"] = "centrality"
        if synthesize and paths:
            result["synthesized_edges"] = _synthesize_edges_for_paths(paths)
        return _finish_code_result(result)

    cap = _semantic_file_memory(_lemoncrow_root())
    if paths:
        for raw in paths:
            candidate = Path(raw)
            if candidate.is_file():
                cap.summarize_file(candidate)
    analytics = cap.graph_analytics()
    if kind == "blast_radius":
        if not path:
            raise ValueError("path is required for kind='blast_radius'")
        result = analytics.blast_radius(str(Path(path)))
    elif kind == "dead_code":
        result = analytics.dead_code(limit=limit)
    elif kind == "cycles":
        result = analytics.cycles(limit=limit)
    elif kind == "topology":
        result = analytics.topology(limit=limit)
    else:  # coupling
        result = analytics.coupling(limit=limit)
    result["kind"] = kind
    _ = render_compact  # file-graph analytics render as JSON; no markdown view
    return result


def _op_graph_code_health(
    *,
    kind: str,
    path: str | None,
    paths: list[str] | None,
    limit: int,
    query: str | None,
    enable: bool | None,
    repo_root: str | None,
) -> dict[str, Any]:
    """Dispatch the WS10 code health & history graph kinds (G15/G16/N17).

    Each delegated function is independently fail-open; this seam only resolves
    the repo/lemoncrow roots and routes by ``kind``.
    """
    from lemoncrow.pro.capabilities.code_health import (
        commit_provenance,
        design_gaps,
        index_design_docs,
        pr_risk,
        recall_design_docs,
        verify_design,
    )

    workspace = _workspace_root()
    repo = (Path(repo_root) if repo_root else workspace).resolve()
    lemoncrow_root = _lemoncrow_root()

    if kind == "design_gaps":
        return design_gaps(repo_root=repo, lemoncrow_root=lemoncrow_root, paths=paths)
    if kind == "verify_design":
        return verify_design(repo_root=repo, lemoncrow_root=lemoncrow_root, paths=paths)
    if kind == "pr_risk":
        targets = paths or ([path] if path else [])
        if not targets:
            raise ValueError("pr_risk requires 'paths' (or 'path') -- the changed files")
        return pr_risk(repo_root=repo, lemoncrow_root=lemoncrow_root, paths=targets)
    if kind == "commit_provenance":
        return commit_provenance(repo_root=repo, path=path, limit=limit)
    if kind == "index_docs":
        return index_design_docs(repo_root=repo, lemoncrow_root=lemoncrow_root, paths=paths, enable=enable)
    # recall_docs
    if not query:
        raise ValueError("recall_docs requires 'query'")
    return recall_design_docs(lemoncrow_root=lemoncrow_root, query=query, limit=limit)


def _op_callers(
    *,
    query: str | None = None,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    language: str | None = None,
    depth: int = 1,
    limit: int = 20,
    snapshot: bool = False,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not any([query, symbol_id, qualified_name, symbol_name]):
        raise ValueError("query, symbol_id, qualified_name, or symbol_name is required for code callers")
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_callers(
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=path,
            kind=kind,
            language=language,
            depth=depth,
            limit=limit,
            snapshot=snapshot,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("callers", payload, render_compact=render_compact))


def _op_callees(
    *,
    query: str | None = None,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    language: str | None = None,
    depth: int = 1,
    limit: int = 20,
    snapshot: bool = False,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not any([query, symbol_id, qualified_name, symbol_name]):
        raise ValueError("query, symbol_id, qualified_name, or symbol_name is required for code callees")
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_callees(
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=path,
            kind=kind,
            language=language,
            depth=depth,
            limit=limit,
            snapshot=snapshot,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("callees", payload, render_compact=render_compact))


def _op_usages(
    *,
    query: str | None = None,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    language: str | None = None,
    file_glob: str | None = None,
    group_by: str = "file",
    snippet_lines: int = 8,
    limit: int = 20,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not any([query, symbol_id, qualified_name, symbol_name]):
        raise ValueError("query, symbol_id, qualified_name, or symbol_name is required for code usages")
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_usages(
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=path,
            kind=kind,
            language=language,
            file_glob=file_glob,
            group_by=group_by,
            snippet_lines=3 if snippet_lines == 8 else snippet_lines,
            limit=limit,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("usages", payload, render_compact=render_compact))


def _op_explore(
    *,
    query: str | None = None,
    seed_files: list[str] | None = None,
    max_files: int = 8,
    max_symbols: int = 4,
    include_source: bool = True,
    include_relationships: bool = True,
    line_numbers: bool = True,
    skeletonize: bool = True,
    complete_families: bool | None = None,
    depth: int = 1,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not query:
        raise ValueError("query is required for code explore")
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_explore(
            query=query,
            seed_files=seed_files,
            max_files=max_files,
            max_symbols=max_symbols,
            include_source=include_source,
            include_relationships=include_relationships,
            line_numbers=line_numbers,
            skeletonize=skeletonize,
            complete_families=complete_families,
            depth=depth,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("explore", payload, render_compact=render_compact))


def _op_pattern(
    *,
    pattern: str | None = None,
    rewrite: str | None = None,
    language: str | None = None,
    file_glob: str | None = None,
    dry_run: bool = True,
    limit: int = 20,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not pattern:
        raise ValueError("pattern is required for code pattern")
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_pattern(
            pattern=pattern,
            rewrite=rewrite,
            language=language,
            file_glob=file_glob,
            dry_run=dry_run,
            limit=limit,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("pattern", payload, render_compact=render_compact))


def _op_search(
    *,
    query: str | None = None,
    mode: str = "auto",
    intent: str = "auto",
    view: Literal["target", "graph", "context", "explain"] = "target",
    kind: str | None = None,
    language: str | None = None,
    snippet: str = "none",
    snippet_lines: int = 8,
    file_glob: str | None = None,
    scope: str = "repo",
    since: str | None = None,
    touched_by: str | None = None,
    provenance: str | None = None,
    seed_files: list[str] | None = None,
    max_symbols: int = 4,
    depth: int = 1,
    limit: int = 20,
    budget_tokens: int = 4000,
    repo: str | None = None,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not query:
        raise ValueError("query is required for code search")
    engine_root = repo_root or "."
    workspace_router = _workspace_code_router(engine_root)
    if repo is not None and not workspace_router.is_configured:
        raise ValueError("repo filter requires .lemoncrow/workspace.toml")
    engine = _code_context_engine(engine_root)
    _code_engine_for_current_call.value = engine
    if workspace_router.is_configured and view in {"graph", "explain", "context"}:
        # The workspace-routed search path only produces a target-shaped payload;
        # graph/explain/context are not routed. Reject explicitly so callers get a
        # clear error instead of a silently-wrong (unrouted, repo-ignoring) result.
        raise ValueError(
            f"view={view!r} is not supported when a workspace is configured (.lemoncrow/workspace.toml); "
            "use view='target' for routed search"
        )
    if view == "context":
        context_payload = engine.tool_context(
            task=query,
            seed_files=seed_files,
            budget_tokens=budget_tokens,
            max_symbols=max_symbols,
        )
        return _finish_code_result(
            _maybe_attach_code_rendered("context", cast(dict[str, Any], context_payload), render_compact=render_compact)
        )
    search_kwargs: dict[str, Any] = {
        "limit": limit,
        "mode": mode,
        "kind": kind,
        "language": language,
        "snippet": snippet,
        "snippet_lines": snippet_lines,
        "file_glob": file_glob,
        "scope": scope,
        "budget_tokens": budget_tokens,
    }
    if scope != "deleted":
        search_kwargs["intent"] = intent
        search_kwargs["seed_files"] = seed_files
    if since is not None:
        search_kwargs["since"] = since
    if touched_by is not None:
        search_kwargs["touched_by"] = touched_by
    if provenance is not None:
        search_kwargs["provenance_filter"] = provenance
    if workspace_router.is_configured:
        routed_payload = cast(
            dict[str, Any],
            workspace_router.route("search", repo=repo, query=query, **search_kwargs),
        )
        routed_hits = _count_search_hits(routed_payload)
        routed_payload = _code_search_target_view(routed_payload)
        routed_result = _finish_code_result(
            _maybe_attach_code_rendered("search", routed_payload, render_compact=render_compact)
        )
        if scope != "repo":
            return routed_result
        return _apply_search_verdict(
            routed_result,
            query=query,
            hit_count=routed_hits,
            channels=engine.search_channel_health(query, mode),
        )
    search_payload = cast(dict[str, Any], engine.tool_search(query, **search_kwargs))
    resolved_mode = str(search_payload.get("mode") or mode)
    primary_hits = _count_search_hits(search_payload)
    text_fallback: list[dict[str, Any]] = []
    if scope == "repo" and primary_hits == 0 and _search_cascade_enabled():
        # Phase 3 -- reactive-serial cascade: ranked search came up empty, so fall
        # through to a literal text scan within the SAME call (catches comments,
        # config, and strings the symbol index does not hold) instead of making
        # the model spend a turn on grep.
        with contextlib.suppress(Exception):
            text_fallback = [
                {"path": match.file_path, "line": match.line} for match in engine.search_text(query, limit=8)
            ]
    if view == "target":
        search_payload = _code_search_target_view(search_payload)
    elif view in {"graph", "explain"}:
        search_payload = _code_search_graph_view(
            engine,
            query=query,
            search_payload=search_payload,
            view=view,
            limit=limit,
            depth=depth,
            budget_tokens=budget_tokens,
        )
    search_result = _finish_code_result(
        _maybe_attach_code_rendered("search", search_payload, render_compact=render_compact)
    )
    if scope != "repo":
        # deleted/external surfaces stay strictly additive -- no verdict stamping.
        return search_result
    if text_fallback:
        search_result["text_fallback"] = text_fallback
    return _apply_search_verdict(
        search_result,
        query=query,
        hit_count=primary_hits or len(text_fallback),
        channels=engine.search_channel_health(query, resolved_mode),
    )


def _op_index(
    *,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    force: bool = False,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_index(
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            force=force,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("index", payload, render_compact=render_compact))


def _op_blame(
    *,
    query: str | None = None,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    path: str | None = None,
    include_churn: bool = True,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    if not (query or symbol_id or qualified_name or symbol_name):
        raise ValueError("query, symbol_id, qualified_name, or symbol_name is required for code blame")
    engine = _code_engine_at(repo_root)
    payload = cast(
        dict[str, Any],
        engine.tool_blame(
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=path,
            include_churn=include_churn,
            budget_tokens=budget_tokens,
        ),
    )
    return _finish_code_result(_maybe_attach_code_rendered("blame", payload, render_compact=render_compact))


def _op_node(
    *,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    path: str | None = None,
    line: int | None = None,
    budget_tokens: int = 4000,
    repo: str | None = None,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    engine_root = repo_root or "."
    workspace_router = _workspace_code_router(engine_root)
    if repo is not None and not workspace_router.is_configured:
        raise ValueError("repo filter requires .lemoncrow/workspace.toml")
    engine = _code_context_engine(engine_root)
    _code_engine_for_current_call.value = engine
    if workspace_router.is_configured:
        payload = cast(
            dict[str, Any],
            workspace_router.route(
                "symbol",
                repo=repo,
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                symbol_name=symbol_name,
                file_path=path,
                line=line,
                budget_tokens=budget_tokens,
            ),
        )
    else:
        payload = cast(
            dict[str, Any],
            engine.tool_symbol(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                symbol_name=symbol_name,
                file_path=path,
                line=line,
                budget_tokens=budget_tokens,
            ),
        )
    return _finish_code_result(_maybe_attach_code_rendered("node", payload, render_compact=render_compact))


def _op_cache_status(
    *,
    cache_tool: str | None = None,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    engine = _code_engine_at(repo_root)
    if cache_tool is None:
        payload = cast(dict[str, Any], engine.tool_cache_status(budget_tokens=budget_tokens))
    else:
        payload = cast(
            dict[str, Any],
            engine.tool_cache_status(cache_tool=cache_tool, budget_tokens=budget_tokens),
        )
    return _finish_code_result(_maybe_attach_code_rendered("cache_status", payload, render_compact=render_compact))


def _op_cache_invalidate(
    *,
    cache_tool: str | None = None,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    engine = _code_engine_at(repo_root)
    if cache_tool is None:
        payload = cast(dict[str, Any], engine.tool_cache_invalidate(budget_tokens=budget_tokens))
    else:
        payload = cast(
            dict[str, Any],
            engine.tool_cache_invalidate(cache_tool=cache_tool, budget_tokens=budget_tokens),
        )
    return _finish_code_result(_maybe_attach_code_rendered("cache_invalidate", payload, render_compact=render_compact))


def _code_intel_handler_hooks() -> CodeIntelHandlerHooks:
    return CodeIntelHandlerHooks(
        graph=_op_graph,
        pattern=_op_pattern,
        index=_op_index,
        blame=_op_blame,
        cache_status=_op_cache_status,
        cache_invalidate=_op_cache_invalidate,
        callers=_op_callers,
        callees=_op_callees,
        usages=_op_usages,
        node=_op_node,
    )


configure_code_intel_handler_hooks(_code_intel_handler_hooks)


# ------------------------------------------------------------------ #
# Dedicated code-intel tools — each calls its `_op_*` engine wrapper  #
# directly (no multiplexer). Published tools carry focused schemas;   #
# repo/admin ops (index, blame, cache) are registered hidden via            #
# the explicit LLM allowlist so tests and power use reach them by name.       #
# ------------------------------------------------------------------ #


def _parse_symbol(symbol: str) -> dict[str, Any]:
    """Route a symbol string to the correct engine kwarg based on form."""
    if "." in symbol:
        return {"qualified_name": symbol}
    return {"symbol_name": symbol}


# Repo/admin code-intel ops — registered but not LLM-allowlisted. Not
# surfaced to agents; reachable by name for tests, the CLI, and power use. Each
# delegates straight to its _op_* engine wrapper.


def _statusline_handler_hooks() -> StatuslineHandlerHooks:
    return StatuslineHandlerHooks(
        lemoncrow_root=_lemoncrow_root,
        session_sidecar=_get_host_session_sidecar_path,
    )


configure_statusline_handler_hooks(_statusline_handler_hooks)


# _DeferredResult, _Deferred and the deferral kill-switch/executor helpers now
# live in mcp.deferral (imported above).
# bash symbols moved to mcp.bash (imported/registered near the top).


# bash symbols moved to mcp.bash (imported/registered near the top).


# bash symbols moved to mcp.bash (imported/registered near the top).


# bash symbols moved to mcp.bash (imported/registered near the top).


def _run_native_grep(
    *,
    path: str,
    content_regex: str | None,
    file_glob_patterns: list[str] | None,
    output_mode: Literal[
        "ranked_file_map",
        "file_paths_with_content",
        "file_paths_only",
        "file_paths_with_match_count",
    ],
    lines_before: int,
    lines_after: int,
    ignore_case: bool,
    type: str | None,
    file_limit: int | None,
    lines_per_file: int | None,
    if_modified_since: str | None,
    multiline: bool,
    summary: bool | None,
    context_budget_tokens: int,
    include_meta: bool,
    badge_provider: Callable[[str, list[str]], str | None] | None = None,
) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.tool_supervision.native_search import search_workspace

    return search_workspace(
        path=path,
        content_regex=content_regex,
        file_glob_patterns=file_glob_patterns,
        output_mode=output_mode,
        lines_before=lines_before,
        lines_after=lines_after,
        ignore_case=ignore_case,
        type=type,
        file_limit=file_limit,
        lines_per_file=lines_per_file,
        if_modified_since=if_modified_since,
        max_line_length=1000,
        multiline=multiline,
        summary=summary,
        context_budget_tokens=context_budget_tokens,
        include_metadata=include_meta,
        repo_root=os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()),
        badge_provider=badge_provider,
    )


# Cap on how many distinct symbols one grep call will badge with call-graph
# counts -- keeps the symbol lookups bounded on large result sets.
_GREP_BADGE_SYMBOL_CAP = 8

# Model-facing grep output-mode names map to the verbose internal output_mode the
# native engine speaks. Short names keep the per-turn schema small.


# Forgiving mode normalisation: the model prefers self-documenting names
# (file_paths_with_content) over the terse canonical ones, so accept both forms
# plus common variants and default unknowns to 'content' -- grep never 422s on mode.


def _grep_badge_provider(rel_path: str, symbol_names: list[str]) -> str | None:
    """Inline relation-count line for a file's matched definition symbols.

    Returns a ` · `-joined badge string (or ``None``) that `search_workspace`
    appends to the file's header so call-graph counts ride along the regex
    search. Uses badge_counts_batch: 3 SQL queries for all symbols instead
    of 3*N serial queries.
    """
    if not symbol_names:
        return None
    repo_root = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    names = symbol_names[:_GREP_BADGE_SYMBOL_CAP]
    try:
        engine = _code_context_engine(repo_root)
        counts = engine.badge_counts_batch(names)
    except Exception:
        return None
    badges: list[str] = []
    for name in names:
        c = counts.get(name, {})
        parts: list[str] = []
        n_callers = int(c.get("callers") or 0)
        n_callees = int(c.get("callees") or 0)
        n_usages = int(c.get("usages") or 0)
        if n_callers:
            parts.append(f"↳{n_callers} callers")
        if n_callees:
            parts.append(f"↰{n_callees} callees")
        if n_usages:
            parts.append(f"⌖{n_usages} usages")
        if parts:
            badges.append(f"{name}  " + " ".join(parts))
    return "  ·  ".join(badges) if badges else None


# --- lean code_search projection --------------------------------------------- #
# code_search is meant to collapse grep->read->edit into a single code_search->edit.
# The engine returns a rich candidate set; handed that whole payload, agents over-
# search and re-read. Project it to a lean, exact view: rank files by best entry-
# point score, return the top files' source verbatim (the code to edit), trim the
# score tail to compact signatures, drop non-actionable metadata. Generic and
# score-relative -- no per-repo tuning. Offline-validated on the real benchmark
# queries: ~64% smaller output, gold-edited file retained in every case.
_LEAN_REL_FLOOR = 0.10
_LEAN_MAX_SOURCE_FILES = 3
_LEAN_MAX_CANDIDATES = 8
# tool_code_search used to expose its own max_files param (hidden from the
# LLM, default 8, 0 real callers ever needed a different value -- narrowing
# happens via paths=/query). Display count was ALREADY fixed regardless: any
# value >=3 gives an identical n_src via min(_LEAN_MAX_SOURCE_FILES, ...)
# below. Collapsed to a plain constant (2026-07-25) rather than a dead
# parameter -- still feeds engine.tool_explore's own internal ranking
# breadth, just no longer externally settable.
_CODE_SEARCH_ENGINE_MAX_FILES = 8
# Candidate-file list is a path-only recall aid (navigation targets). 24 costs
# a measured 0.8-1.5k chars resident per turn -- but a FLAT trim to 10, then 6
# was part of a measured -0.10 overall retrieval MRR regression (2026-07-06):
# the tail is where non-top-1 golds live, so it earns its chars in the general
# (ambiguous / multi-candidate) case. Do not flat-trim this constant without
# re-running `lc eval retrieval`. The symbol map (_LEAN_MAX_CANDIDATES) stays
# tight because each entry carries structure.
# 24 -> 16 -> 8 (2026-07-25): re-testing the flat-trim ceiling now that the
# dominant-case cap, doc-diversity demotion, and inline gating all exist --
# none of which were present for the 2026-07-06 10/6 regression above. 8 sits
# INSIDE that historical regression's range (10 then 6 both regressed -0.10
# MRR combined) -- 16->8 is the real test of whether the old cause still
# applies under the current architecture; 24->16 alone measured -0.0008 MRR
# (noise-floor, hit@1 unchanged). Gated by the same `lc eval retrieval`
# re-run rule; revert to 16 (or 24) if THIS step regresses for real.
_LEAN_MAX_CANDIDATE_FILES = 8
# Dynamic exception, not a flat trim: only when `_lean_code_search_view`'s
# `dominant` signal fires (exact match, no seed scope, one file scoring
# >=4x its nearest rival, or the only scored file) is the tail this cheap to
# cut -- the same exact-single-winner case that already collapses `files` to
# n_src=1. Ambiguous/no-exact-match cases keep the full 8-wide net.
# Deliberately NOT keyed on any per-candidate score (e.g. epscore==0): that
# was tried and falsified by
# test_lean_code_search_view_candidates_keep_symbol_map_paths's fresh.py case
# (epscore==0 means "no entry_points row", not "irrelevant") -- this instead
# reuses the already-validated whole-response `dominant` confidence signal.
# 8 -> 5 (2026-07-25): the dominant case is already the confident
# single-winner path (files already collapse to n_src=1) -- narrower blast
# radius than the general cap, so trimmed further first. Gated by the same
# `lc eval retrieval` re-run rule; revert to 8 if MRR regresses.
_LEAN_CANDIDATE_FILES_DOMINANT = 5
_LEAN_DOMINANT_RATIO = 4.0
# Tried a dynamic quality gate here (drop candidate_files entries with
# epscore==0.0) and reverted before it reached eval: epscore is populated
# ONLY from entry_points/symbol-level matches, so a file reaching `files` via
# a channel that never emits an entry_points row (pure lexical/path match, no
# symbol pinned) scores 0.0 despite being a real candidate --
# test_lean_code_search_view_candidates_keep_symbol_map_paths's `fresh.py`
# case is exactly this, and it's hardened from the same 2026-07-06 MRR
# regression this whole cand_files block warns about. A sound dynamic gate
# needs a real per-candidate confidence score unified across every surfacing
# channel (not just entry_points) -- that's engine-level work, not available
# at this layer today.


def _lean_score(sym: dict[str, Any]) -> float:
    try:
        return float(sym.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _lean_related_symbol(sym: dict[str, Any]) -> dict[str, Any]:
    name = sym.get("qualified_name") or sym.get("name")
    if isinstance(name, str):
        # Markdown headings can index a whole frontmatter block as their "name";
        # a related_symbols entry is a one-line pointer, never a prose dump.
        name = name.split("\n", 1)[0].strip()
        if len(name) > 120:
            name = name[:117] + "..."
    result: dict[str, Any] = {
        "qualified_name": name,
        "path": sym.get("path"),
        "line": sym.get("line"),
        "end_line": sym.get("end_line"),
        "kind": sym.get("kind"),
        "repo_name": sym.get("repo_name"),
        "project_id": sym.get("project_id"),
    }
    return {key: value for key, value in result.items() if value is not None}


def _lean_section(sec: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": sec.get("path"),
        "qualified_name": sec.get("qualified_name") or sec.get("name"),
        "line": sec.get("line"),
        "end_line": sec.get("end_line"),
        "content": sec.get("content", ""),
    }


# Guards the whole-file fallback below against reading a pathological huge
# file into memory -- downstream _outline_lean_view already caps rendered
# size (_CODESEARCH_TOP2_MAX_CHARS), this only bounds the raw read.
_CODESEARCH_FALLBACK_MAX_BYTES = 2_000_000


def _whole_file_fallback_section(workspace_root: Path, relpath: str) -> dict[str, Any] | None:
    """Whole-file source entry for a resolved-path code_search hit whose file
    type carries no symbol index (shell scripts, markdown, config, ...), so
    the engine's normal symbol pipeline returns zero source_sections for it.
    Without this, a fast-path pin to a non-code file would resolve exact_match
    correctly but ship no content -- the caller would still have to `read` it
    themselves. Downstream outline/top2 shaping still bounds the size.
    """
    target = workspace_root / relpath
    try:
        if target.stat().st_size > _CODESEARCH_FALLBACK_MAX_BYTES:
            return None
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {
        "path": relpath,
        "sections": [
            {
                "path": relpath,
                "qualified_name": None,
                "line": 1,
                "end_line": text.count("\n") + 1,
                "content": text,
            }
        ],
    }


# Baseline for the token credit = the source you'd realistically READ (the
# returned section + comparable surrounding context), not the whole file.
_REALISTIC_READ_FACTOR = 2


# Per-session set of (absolute) file paths already credited a code_search read /
# token saving. A repeat search returning the same file re-reads nothing — you
# already have it — so credit each file once per session, not once per search.
_SEARCH_CREDITED_PATHS: dict[str, set[str]] = {}


def _code_search_section_savings(lean: dict[str, Any], workspace_root: Path) -> tuple[int, int]:
    """Estimate the round-trips a code_search replaced vs. a manual grep+read loop.

    Credited ONCE per file per session: a later search returning a file you
    already pulled re-reads nothing.

    A single ``grep``/``glob`` returns matches across ALL files at once, so the
    primary locate is a 1-for-1 swap with the code_search call — NOT one avoided
    call per file. What the call genuinely avoids is a measured lower bound:
      * one ``Read`` per file whose source came back whole (a section cut short
        with a ``[truncated …]`` marker is a preview you might still open the
        file for, so it isn't counted); plus
      * one extra locate (grep for usages) when the symbol map points to files
        beyond the returned source — a second query you'd otherwise run.
    """
    files = lean.get("files")
    if not isinstance(files, list):
        return 0, 0
    try:
        session = _resolved_host_session_id() or ""
    except Exception:
        session = ""
    credited = _SEARCH_CREDITED_PATHS.setdefault(session, set())

    tokens_saved = 0
    reads_saved = 0
    source_paths: set[str] = set()  # files with source THIS call (nav gating)
    for entry in files:
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        sections = entry.get("sections")
        if not isinstance(sections, list) or not sections:
            continue
        file_content = ""
        for section in sections:
            if isinstance(section, dict):
                content = section.get("content")
                if isinstance(content, str):
                    file_content += content
        source_paths.add(raw_path)
        candidate = Path(raw_path)
        full_path = candidate if candidate.is_absolute() else workspace_root / candidate
        key = str(full_path)
        if key in credited:
            continue  # already pulled this session — a repeat search re-reads nothing
        try:
            file_size = min(full_path.stat().st_size, _HOST_INLINE_RESULT_CHARS)
        except OSError:
            continue
        credited.add(key)
        # A Read is avoided only when the source came back whole; a truncated
        # outline is a preview, not a replaced read — keep the count a lower bound.
        if file_content and "[truncated" not in file_content:
            reads_saved += 1
        # Credit the source you'd realistically have read (section + comparable
        # surrounding context), not the entire file: a targeted read opens the
        # symbol's neighborhood, not a 100k-line file.
        actual = len(file_content)
        tokens_saved += max(0, (min(file_size, _REALISTIC_READ_FACTOR * actual) - actual) // 4)

    # One extra locate (grep for usages) is avoided only when the symbol map
    # points to files beyond the ones we returned source for — otherwise it's
    # the same files you already have, not a second query.
    nav_saved = 0
    related = lean.get("related_symbols")
    if isinstance(related, list):
        for sym in related:
            if isinstance(sym, dict):
                p = sym.get("path")
                if isinstance(p, str) and p and p not in source_paths:
                    nav_saved = 1
                    break
    calls_saved = reads_saved + nav_saved
    return tokens_saved, calls_saved


def _lean_code_search_view(
    result: dict[str, Any],
    *,
    max_files: int,
    seed_files: list[str] | None = None,
    query: str = "",
    max_candidates: int | None = None,
) -> dict[str, Any]:
    """Collapse engine.tool_explore output into a lean, edit-ready view."""
    if not isinstance(result, dict):
        return result
    # Filetype diversity: unless the query asks for docs, documentation files
    # (.md/.rst/...) may occupy at most a quarter of each ranked window --
    # excess docs are demoted below it, never dropped from the surface order.
    wants_docs = query_wants_docs(query)
    eps = sorted((result.get("entry_points") or []), key=_lean_score, reverse=True)
    files = result.get("files") or []
    exact = bool(result.get("exact_match"))

    epscore_by_path: dict[str, float] = {}
    for e in eps:
        p = e.get("path")
        if p is not None:
            epscore_by_path[p] = max(epscore_by_path.get(p, 0.0), _lean_score(e))

    seed_norm = {s.rstrip("/") for s in (seed_files or [])}

    def is_seed(path: str | None) -> bool:
        if not path:
            return False
        return any(path == s or path.startswith(s + "/") for s in seed_norm)

    def _fscore(f: dict[str, Any]) -> float:
        fp = f.get("path")
        return epscore_by_path.get(fp if isinstance(fp, str) else "", 0.0)

    # A learned explore reranker already produced the file order. Preserve that
    # order through the MCP projection instead of immediately overwriting it
    # with legacy entry-point scores; otherwise the reranker is effectively a
    # no-op on the agent-visible `files + candidate_files` ranking. This changes
    # ordering only — the lean response schema is untouched.
    experiment = result.get("experiment")
    learned_order = isinstance(experiment, dict) and str(experiment.get("name") or "").startswith("explore_reranker_")

    # Reserve up to the top-2 spots for in-scope files that actually matched
    # (epscore > 0), highest first; every other file -- out-of-scope neighbours the
    # whole-repo channels surfaced, plus any 3rd+ in-scope file -- races purely on
    # score below them. An unmatched in-scope file claims no reserved slot, so a
    # 0-match seed lets a neighbour take rank #1.
    if learned_order:
        _reserved = [f for f in files if is_seed(f.get("path")) and _fscore(f) > 0.0][:2]
        _reserved_ids = {id(f) for f in _reserved}
        _rest = [f for f in files if id(f) not in _reserved_ids]
    else:
        _reserved = sorted(
            (f for f in files if is_seed(f.get("path")) and _fscore(f) > 0.0),
            key=_fscore,
            reverse=True,
        )[:2]
        _reserved_ids = {id(f) for f in _reserved}
        _rest = sorted((f for f in files if id(f) not in _reserved_ids), key=_fscore, reverse=True)
    ranked = [*_reserved, *_rest]
    # Dominance controls source volume, not file relevance. Keep it anchored to
    # the legacy entry-point confidence even when the learned model changes file
    # order; otherwise a reranked low-epscore file at rank 1 turns an exact-hit
    # response from one source file into up to `max_files` source files.
    dominance_ranked = sorted(files, key=_fscore, reverse=True) if learned_order else ranked
    top_fs = epscore_by_path.get(dominance_ranked[0].get("path"), 0.0) if dominance_ranked else 0.0
    second_fs = epscore_by_path.get(dominance_ranked[1].get("path"), 0.0) if len(dominance_ranked) > 1 else 0.0
    dominant = (
        exact and not seed_norm and top_fs > 0.0 and (second_fs == 0.0 or top_fs >= _LEAN_DOMINANT_RATIO * second_fs)
    )
    # Ranking surface is sacred: WHICH files carry sections feeds retrieval
    # rank (and the MRR eval). Context cost of extra sections is handled by
    # outline shaping (pointer-only past the cap), never by dropping files
    # here -- gating this on exact_match measured -0.10 overall MRR (2026-07-06).
    n_src = 1 if dominant else min(_LEAN_MAX_SOURCE_FILES, max(1, max_files))

    out_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for f in ranked[:n_src]:
        secs = [_lean_section(s) for s in (f.get("source_sections") or [])]
        if secs:
            entry = {"path": f.get("path"), "sections": secs}
            if f.get("repo_name") is not None:
                entry["repo_name"] = f.get("repo_name")
            if f.get("project_id") is not None:
                entry["project_id"] = f.get("project_id")
            out_files.append(entry)
            seen_paths.add(f.get("path"))
    if not out_files:  # never return zero source when some file has sections
        for f in ranked:
            secs = [_lean_section(s) for s in (f.get("source_sections") or [])]
            if secs:
                entry = {"path": f.get("path"), "sections": secs}
                if f.get("repo_name") is not None:
                    entry["repo_name"] = f.get("repo_name")
                if f.get("project_id") is not None:
                    entry["project_id"] = f.get("project_id")
                out_files.append(entry)
                seen_paths.add(f.get("path"))
                break
    # Cross-file symbol map: top-K entry points as structured ranges. On
    # multi-file tasks this lets callers navigate related sites without a second
    # symbol-resolution call. Symbols whose exact span already came back as
    # source are skipped -- repeating a returned section as a pointer is noise.
    returned_spans: set[tuple[Any, Any, Any]] = set()
    for out in out_files:
        for sec in out.get("sections") or []:
            returned_spans.add((out.get("path"), sec.get("line"), sec.get("end_line")))
    candidates: list[dict[str, Any]] = []
    seen_sig: set[tuple[Any, Any, Any, Any]] = set()
    for e in eps:
        candidate = _lean_related_symbol(e)
        if (candidate.get("path"), candidate.get("line"), candidate.get("end_line")) in returned_spans:
            continue
        sig = (
            candidate.get("qualified_name"),
            candidate.get("path"),
            candidate.get("line"),
            candidate.get("end_line"),
        )
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        candidates.append(candidate)
        # Scan past the window so the diversity pass below has non-doc
        # candidates to promote when docs dominate the top scores.
        if len(candidates) >= _LEAN_MAX_CANDIDATES * 3:
            break
    if not wants_docs:
        # Effective window: the list head IS the ranked space when the list is
        # shorter than the cap.
        candidates = demote_doc_overflow(
            candidates,
            window=min(_LEAN_MAX_CANDIDATES, len(candidates)),
            path_of=lambda c: str(c.get("path") or ""),
        )
    candidates = candidates[:_LEAN_MAX_CANDIDATES]
    # Exclude ONLY files whose source was returned (seen_paths). Files that
    # appear in the symbol map STAY in candidate_files: candidate_files is the
    # ranked recall surface (the retrieval eval and rank-consumers read files +
    # candidate_files, never the symbol map) -- excluding map-covered paths
    # deleted the top entry-point files from the ranking and collapsed hit@3
    # (part of a measured -0.10 overall MRR, 2026-07-06).
    cand_files: list[str] = []
    for f in ranked:
        p = f.get("path")
        if p and p not in seen_paths and p not in cand_files:
            cand_files.append(p)
    for p in result.get("additional_relevant_files") or []:
        if p and p not in seen_paths and p not in cand_files:
            cand_files.append(p)
    # Recall tails (fused cross-channel matches, then a deeper lexical pass),
    # appended STRICTLY LAST so the primary candidate order is unchanged. NOT
    # gated on exact_match: exact_match only says SOME exact symbol name
    # matched, not that the gold file surfaced -- gating these on it measured
    # part of the same -0.10 MRR regression.
    for p in result.get("fused_recall") or []:
        if p and p not in seen_paths and p not in cand_files:
            cand_files.append(p)
    for p in result.get("deep_recall") or []:
        if p and p not in seen_paths and p not in cand_files:
            cand_files.append(p)

    # Dynamic cap: the exact-single-winner case (`dominant`, same signal that
    # already collapses `files` to one) also shrinks the candidate_files tail
    # -- see _LEAN_CANDIDATE_FILES_DOMINANT for why this is safe where a flat
    # trim was not. Ambiguous/multi-candidate cases keep the full 8-wide net.
    # An explicit max_candidates from the caller overrides both defaults --
    # the agent knows its own confidence better than either heuristic (e.g.
    # "just the top hit" after it's already narrowed the file itself).
    if max_candidates is not None:
        cand_cap = max(1, max_candidates)
    else:
        cand_cap = _LEAN_CANDIDATE_FILES_DOMINANT if dominant else _LEAN_MAX_CANDIDATE_FILES
    if cand_files and not wants_docs:
        cand_files = demote_doc_overflow(cand_files, window=min(cand_cap, len(cand_files)))

    lean: dict[str, Any] = {"exact_match": exact, "files": out_files}
    if candidates:
        lean["related_symbols"] = candidates
    if cand_files:
        visible_candidates = cand_files[:cand_cap]
        lean["candidate_files"] = visible_candidates
        project_by_path = result.get("project_by_path")
        if isinstance(project_by_path, dict):
            candidate_projects = {
                path: project_by_path[path] for path in visible_candidates if isinstance(project_by_path.get(path), str)
            }
            if candidate_projects:
                lean["candidate_projects"] = candidate_projects
        # The cap silently cutting the ranked tail would look identical to
        # "nothing else matched" -- name the count still hidden and how to
        # raise the cap, so the agent can ask for more instead of assuming
        # recall stopped here.
        _hidden = len(cand_files) - cand_cap
        if _hidden > 0:
            lean["candidate_files_more"] = _hidden
    if result.get("truncated"):
        lean["truncated"] = True
    return lean


def _attach_code_search_savings(view: dict[str, Any], workspace_root: Path | None = None) -> dict[str, Any]:
    """Attach tokens_saved/calls_saved AFTER outline shaping.

    Savings must be measured on the view the model actually receives: an
    outlined or head-capped section is a pointer the agent may still read, not
    a replaced Read -- crediting the pre-shaped source would overcount exactly
    when outline mode trims the most.
    """
    root = workspace_root or _workspace_root()
    # Inline source = already read: files whose sections were served count as
    # reads for the blind-range-edit freshness ledger too.
    for _entry in view.get("files") or []:
        if isinstance(_entry, dict) and _entry.get("sections"):
            _p = _entry.get("path")
            if isinstance(_p, str) and _p:
                _record_read_sig(_p if Path(_p).is_absolute() else root / _p)
    tokens_saved, calls_saved = _code_search_section_savings(view, root)
    if tokens_saved > 0:
        view["tokens_saved"] = tokens_saved
    if calls_saved > 0:
        view["calls_saved"] = calls_saved
    return view


def _observe_code_search_evidence(
    *,
    query: str,
    payload: dict[str, Any],
    engine: Any | None = None,
    seed_files: list[str] | None = None,
    max_files: int = _CODE_SEARCH_ENGINE_MAX_FILES,
) -> dict[str, Any]:
    """Emit evidence facts and optionally execute one benchmark-only expansion."""

    import time

    from lemoncrow.pro.capabilities.code_context.search_verdict import ChannelHealth

    result = payload
    try:
        channels = engine.search_channel_health(query, "auto") if engine is not None else ChannelHealth()
        requested = tuple(name for name in ("semantic", "zoekt") if getattr(channels, name) is not None)
        live = tuple(name for name in requested if getattr(channels, name) is True)
        state = evaluate_explore_evidence(
            result,
            budget_tokens=2000,
            channels_requested=requested,
            channels_live=live,
            dark_channels=channels.dark(),
        )
        ledger = _get_ledger()
        session_id = _get_claude_session_id() or "_global"
        ledger.record_runtime_decision(state.to_runtime_event(session_id=session_id))
        proposal = propose_evidence_resolution(state)
        actual_action: EvidenceResolutionAction = "STOP"
        rounds = 0
        elapsed_ms = 0

        if proposal.mode == "experiment" and proposal.eligible and engine is not None:
            started = time.monotonic()
            if proposal.action == "EXPAND_RELATIONS":
                candidate = engine.tool_explore(
                    query,
                    max_files=max_files,
                    seed_files=seed_files,
                    include_source=True,
                    include_relationships=True,
                    depth=1,
                    budget_tokens=proposal.token_budget,
                )
            elif proposal.action == "HYDRATE_SOURCE":
                candidate = engine.tool_explore(
                    query,
                    max_files=max_files,
                    seed_files=seed_files,
                    include_source=True,
                    budget_tokens=proposal.token_budget,
                )
            else:
                candidate = None
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if isinstance(candidate, dict):
                result = candidate
                actual_action = proposal.action
                rounds = 1

        if proposal.mode != "off":
            ledger.record_runtime_decision(
                proposal.to_runtime_event(
                    state,
                    session_id=session_id,
                    actual_action=actual_action,
                    rounds=rounds,
                    elapsed_ms=elapsed_ms,
                )
            )
    except Exception:
        logging.debug("code-search evidence observation failed", exc_info=True)
    return result


def _search_handler_hooks() -> SearchHandlerHooks:
    return SearchHandlerHooks(
        workspace_root=_workspace_root,
        code_context_engine=_code_context_engine,
        run_native_grep=_run_native_grep,
        grep_badge_provider=_grep_badge_provider,
        apply_search_verdict=_apply_search_verdict,
        count_grep_hits=count_grep_hits,
        check_repeat_query=_check_repeat_query,
        workspace_code_router=_workspace_code_router,
        resolve_query_as_existing_file=_resolve_query_as_existing_file,
        lean_code_search_view=_lean_code_search_view,
        whole_file_fallback_section=_whole_file_fallback_section,
        outline_lean_view=_outline_lean_view,
        attach_code_search_savings=_attach_code_search_savings,
        observe_code_search_evidence=_observe_code_search_evidence,
        code_search_engine_max_files=_CODE_SEARCH_ENGINE_MAX_FILES,
    )


configure_search_handler_hooks(_search_handler_hooks)


# Broker execution is transport-independent and lives in gateway.tools.broker.
# Keep this wrapper so embedded callers that reference the old private name do
# not break while the transport is decomposed.
def _tool_broker_handler(args: dict[str, Any]) -> dict[str, Any] | Any:
    return invoke_tool_broker(
        args,
        renderer=render_tool_result_text,
        visibility=_tool_visible_to_llm,
        description=_tool_description,
    )


_TOOL_BROKER_SPEC: dict[str, Any] = {**TOOL_BROKER_SURFACE_SPEC, "handler": _tool_broker_handler}


# --------------------------------------------------------------------------- #
# Remote mode & dispatcher                                                    #
# --------------------------------------------------------------------------- #
# Remote-routed tool names live in gateway.tools.invocation.

# Read-only tools for outcome tracking (distinguishes reads from writes).
_READ_TOOLS = frozenset(
    {
        "Read",
        "View",
        "read_file",
        "view",
        "view_range",
        "search_read",
        "grep",
        "glob",
        "cached_grep",
    }
)


def _defer_edit_hooks() -> bool:
    """Move mutating edit-hooks (format / organize-imports) and contract-site
    re-fires to the Stop hook. Default off. Read fresh each call (never cached, so
    the env can change at runtime and tests can monkeypatch it)."""
    return os.environ.get("LEMONCROW_DEFER_EDIT_HOOKS", "0").strip().lower() in {"1", "true", "on", "yes"}


# Per-session contract-literal sites already surfaced to the agent, so a site it is
# mid-way through fixing does not re-fire on every later edit (noise). Keyed by
# session id; the Stop hook re-checks the final tree for the real omissions.
_CONTRACT_SEEN: dict[str, set[str]] = {}


def _contract_surface_once(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only contract sites not yet surfaced this session; record them."""
    sid = ""
    with contextlib.suppress(Exception):
        sid = _get_ledger().session_id or ""
    seen = _CONTRACT_SEEN.setdefault(sid, set())
    fresh = [s for s in sites if isinstance(s, dict) and s.get("path") not in seen]
    for s in fresh:
        p = s.get("path")
        if isinstance(p, str):
            seen.add(p)
    return fresh


# Flat single-object schema. The Anthropic Messages API rejects a top-level
# oneOf/anyOf/allOf in a tool's input_schema, so Claude Code's MCP client
# silently SKIPS any tool whose schema uses one ("its input schema uses
# top-level oneOf, which the Anthropic API does not accept") -- which is why a
# previous action-branched oneOf shape made `bash` vanish from the tool list
# while every other tool stayed. Per-action requirements (command for run,
# session_id for poll/kill) are enforced in _run_bash_tool, not the schema.
# BASH_TOOL_INPUT_SCHEMA moved to mcp.bash.


# bash symbols moved to mcp.bash (imported/registered near the top).


# tool_web_fetch now lives in mcp.tools_commodity (imported near the top).
# tool_mcp now lives in mcp.tools_commodity (imported near the top).
_remote_client: Any = None


def _get_remote_client() -> Any:
    global _remote_client
    with _STATE_LOCK:
        if _remote_client is None:
            from lemoncrow.gateway.transports.service import ServiceTransport

            _remote_client = ServiceTransport()
    return _remote_client


def _dispatch_remote(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if _remote_client is None and not os.environ.get("LEMONCROW_SERVICE_URL"):
        if name == "context":
            # Route through the registered handler so bootstrap job queuing,
            # worker spawn throttle, and session bookkeeping all execute.
            spec = TOOLS.get("context")
            if spec is None:
                raise ValueError("context tool not registered")
            handler = cast(Callable[[dict[str, Any]], dict[str, Any]], spec["handler"])
            return handler(args)
        if name == "rescue":
            rescue_result = _runtime().rescue_failure(
                task=str(args.get("task") or ""),
                error=str(args.get("error") or ""),
                files=cast(list[str], args.get("files") or []),
                recent_actions=cast(list[str], args.get("recent_actions") or []),
                domain=cast(str | None, args.get("domain")),
            )
            return rescue_result.model_dump()
        spec = TOOLS.get(name)
        if spec is None:
            raise ValueError(f"unknown remote tool: {name}")
        handler = cast(Callable[[dict[str, Any]], dict[str, Any]], spec["handler"])
        return handler(args)
    client = _get_remote_client()

    if name == "context":
        context_args = dict(args)
        context_args["files"] = cast(list[str], args.get("files") or [])
        context_args["tools"] = cast(list[str], args.get("tools") or [])
        context_args["errors"] = cast(list[str], args.get("errors") or [])
        return cast(dict[str, Any], client.get_context(context_args))
    if name == "memory":
        return cast(dict[str, Any], client.memory(args))
    if name == "rescue":
        return cast(dict[str, Any], client.rescue_failure(args))
    if name in {"trace", "record"}:
        trace_result = cast(dict[str, Any], client.record_trace(args))
        trace_id = str(trace_result.get("trace_id") or trace_result.get("id") or "")
        event_recorded = bool(trace_result.get("event_recorded"))
        return {"trace_id": trace_id, "event_recorded": event_recorded}
    if name == "verify":
        return cast(dict[str, Any], client.run_rubric_gate(args))
    raise ValueError(f"tool not supported in remote mode: {name}")


# --------------------------------------------------------------------------- #
# MCP Protocol Handling                                                       #
# --------------------------------------------------------------------------- #


def _lever_for_tool(tool_name: str) -> str:
    lowered = tool_name.strip().lower().replace("-", "_").replace(" ", "_")
    if lowered in {"read", "search"} or lowered.endswith("_read") or lowered.endswith("_search"):
        return "search_read"
    if lowered == "edit" or lowered.endswith("_edit"):
        return "batch_edit"
    if lowered == "sql" or lowered.endswith("_sql"):
        return "sql_batch"
    if lowered == "compact" or lowered.endswith("_compact"):
        return "compact_lifecycle"
    if lowered == "memory" or lowered.endswith("_memory"):
        return "scoped_recall"
    if lowered == "context" or lowered.endswith("_context"):
        return "playbook_inject"
    return lowered or "unknown"


def _price_tokens_saved_usd(model: str, tokens_saved: int, *, long_context: bool = False) -> float:
    """Price ``tokens_saved`` at *model*'s INPUT rate. No fallback.

    Saved tokens are bytes LemonCrow kept out of the LLM input — they would
    have been billed as new input tokens at the model in use at that turn,
    at the >200k premium input rate when that request ran long-context.
    If the model is unknown or has no pricing entry, returns 0.0 (no guess).
    """
    if tokens_saved <= 0 or not model or model == "_default":
        return 0.0
    from lemoncrow.core.capabilities.pricing import get_model_pricing

    pricing = get_model_pricing(model)
    if pricing is None or not pricing.known or pricing.input <= 0:
        return 0.0
    return pricing.request_cost_usd(input_tokens=int(tokens_saved), long_context=long_context)


def _classify_read_savings(
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    tokens_saved: int,
    default_lever: str,
) -> tuple[str, dict[str, Any]]:
    lowered = tool_name.strip().lower().replace("-", "_").replace(" ", "_")
    if lowered not in {"read", "smart_read"}:
        return default_lever, {}

    mode = str(result.get("mode") or "").strip().lower()
    if mode == "outline" and tokens_saved > 0:
        classified = "structure_map"
    elif mode == "range" and tokens_saved > 0:
        classified = "delta_read"
    else:
        classified = default_lever

    path = result.get("path") or args.get("file_path") or args.get("path")
    metadata: dict[str, Any] = {"read_mode": mode or "full"}
    if isinstance(path, str) and path:
        metadata["path"] = path
    range_spec = result.get("range") or args.get("range")
    if isinstance(range_spec, str) and range_spec:
        metadata["range"] = range_spec
    if "cache_hit" in result:
        metadata["cache_hit"] = bool(result.get("cache_hit"))
    return classified, metadata


def _record_context_budget_for_tool(
    tool_name: str,
    args: dict[str, Any],
    led: RunLedger,
    result: dict[str, Any],
    *,
    rendered_text_size: int | None = None,
) -> None:
    try:
        recorder = _get_context_budget_recorder()

        # Model is best-effort for the analytics recorder below; the
        # response-embedded `saved` field carries the per-event truth.
        model = str(getattr(led, "model", "") or os.environ.get("LEMONCROW_MODEL") or "").strip()

        compact_tool_tokens_saved = _extract_compact_output_tokens_saved(result)
        tokens_saved = _extract_tokens_saved(result)
        base_lever = _lever_for_tool(tool_name)
        lever, savings_metadata = _classify_read_savings(
            tool_name,
            args if isinstance(args, dict) else {},
            result,
            tokens_saved=tokens_saved,
            default_lever=base_lever,
        )
        if "cache_hit" in result and "cache_hit" not in savings_metadata:
            savings_metadata["cache_hit"] = bool(result.get("cache_hit"))
        if isinstance(result.get("provenance"), str):
            savings_metadata.setdefault("provenance", str(result["provenance"]))
        op = args.get("op") if isinstance(args, dict) else None
        if isinstance(op, str) and op:
            savings_metadata.setdefault("op", op)

        raw_lever_savings = result.get("tokens_saved")
        lever_savings = raw_lever_savings.copy() if isinstance(raw_lever_savings, dict) else {}
        if compact_tool_tokens_saved > 0 and not lever_savings:
            lever_savings[f"compact_tool_output:{lever}"] = compact_tool_tokens_saved
        elif tokens_saved > 0:
            lever_savings[lever] = max(int(lever_savings.get(lever, 0) or 0), tokens_saved)
        if tool_name:
            lever_savings.setdefault(f"tool:{tool_name}", 0)

        # Lifetime smart-state counters remain useful for cumulative "savings
        # since install" metrics; they're a single integer pair, not a
        # per-event log. Real per-session savings ride the MCP response's
        # content[].saved field into the Claude transcript.
        calls_avoided = _coerce_saved_tokens(result.get("calls_saved"))
        if tokens_saved > 0 or calls_avoided > 0:
            _record_smart_state_savings(tokens_saved=tokens_saved, calls_avoided=calls_avoided)

        actual_output_tokens = int(result.get("total_tokens", 0) or 0)
        if actual_output_tokens <= 0:
            if rendered_text_size is not None:
                actual_output_tokens = max(0, rendered_text_size // 4)
            else:
                actual_output_tokens = max(0, len(json.dumps(result, ensure_ascii=False, default=str)) // 4)

        if compact_tool_tokens_saved > 0 and not isinstance(raw_lever_savings, dict):
            recorder.record_compact_tool_output(
                session_id=led.session_id,
                turn_index=max(0, len(led.events) - 1),
                model=model,
                method=lever,
                tokens_in=actual_output_tokens + compact_tool_tokens_saved,
                tokens_out=actual_output_tokens,
            )
        else:
            recorder.record(
                session_id=led.session_id,
                turn_index=max(0, len(led.events) - 1),
                model=model,
                input_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=actual_output_tokens,
                naive_input_tokens=actual_output_tokens + tokens_saved,
                lever_savings=lever_savings,
                tool_calls=1,
            )
    except Exception:
        logging.exception("Recovered from broad exception handler")
        logger.warning("Suppressed exception while recording context budget", exc_info=True)


def _model_routing_hooks() -> ModelRoutingHooks:
    return ModelRoutingHooks(
        read_workspace_state=_read_workspace_session_state,
        write_workspace_state=_write_workspace_session_state,
        state_lock=_STATE_LOCK,
        select_owned_execution_route=_select_owned_execution_route,
        detect_agent=_detect_agent,
        append_live_savings_event=_append_live_savings_event,
        get_host_session_sidecar_path=_get_host_session_sidecar_path,
        make_outcome_writer=_make_outcome_writer,
        latest_cache_affinity_model=_latest_cache_affinity_model,
        ledger_turn_count=_ledger_turn_count,
    )


def _workflow_state_from_workspace() -> dict[str, Any]:
    return _canonical_workflow_state_from_workspace(_model_routing_hooks())


def _persist_legacy_route(workflow: dict[str, Any], payload: dict[str, Any], current_step: str) -> None:
    _canonical_persist_legacy_route(_model_routing_hooks(), workflow, payload, current_step)


def _model_recommendation_state(led: RunLedger, args: dict[str, Any]) -> dict[str, Any]:
    return _canonical_model_recommendation_state(_model_routing_hooks(), led, args)


def _prepare_model_recommendation(
    tool_name: str,
    args: dict[str, Any],
    led: RunLedger,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    return _canonical_prepare_model_recommendation(_model_routing_hooks(), tool_name, args, led)


def _finalize_model_recommendation(
    payload: dict[str, Any],
    *,
    led: RunLedger,
    tool_name: str,
    session_state: Mapping[str, Any],
    workflow: dict[str, Any],
    current_step: str,
    wrapper_applied: bool = False,
    wrapper_model: str | None = None,
) -> dict[str, Any]:
    return _canonical_finalize_model_recommendation(
        _model_routing_hooks(),
        payload,
        led=led,
        tool_name=tool_name,
        session_state=session_state,
        workflow=workflow,
        current_step=current_step,
        wrapper_applied=wrapper_applied,
        wrapper_model=wrapper_model,
    )


def _latest_cache_affinity_model(led: RunLedger) -> str | None:
    for event in reversed(led.events):
        payload = event.payload
        raw_cache_write_tokens = (
            payload.get("cache_write_tokens")
            or payload.get("cache_creation_input_tokens")
            or payload.get("cache_creation_tokens")
            or 0
        )
        try:
            cache_write_tokens = int(raw_cache_write_tokens)
        except (TypeError, ValueError):
            cache_write_tokens = 0
        model = str(payload.get("model") or "").strip()
        if cache_write_tokens > 0 and model:
            return model
    return None


def _estimate_compacted_state_tokens(state: Any) -> int:
    prompt_block = state.to_prompt_block()
    preserved_chars = len(prompt_block) + sum(len(turn) for turn in state.recent_turns)
    return max(0, preserved_chars // 4)


def _session_compaction_savings_payload(
    led: RunLedger,
    state: Any,
    *,
    tokens_before: int,
    trigger: str,
    reason: str,
    utilisation_pct: float | None = None,
) -> dict[str, Any]:
    tokens_after_estimate = _estimate_compacted_state_tokens(state)
    tokens_freed = max(0, int(tokens_before) - tokens_after_estimate)
    model = (
        _latest_cache_affinity_model(led)
        or str(getattr(led, "model", "") or "").strip()
        or os.environ.get("LEMONCROW_MODEL", "")
    ).strip()
    # A window already past the >200k threshold was billing premium rates on
    # every request — the tokens this compaction freed are worth the same.
    long_ctx = _savings_long_context(model, int(tokens_before))
    cost_saved_usd = round(_price_tokens_saved_usd(model, tokens_freed, long_context=long_ctx), 6)
    utilisation = (
        round(float(utilisation_pct), 1)
        if utilisation_pct is not None
        else round(100.0 * max(0, int(tokens_before)) / CONTEXT_WINDOW_TOKENS, 1)
    )
    return {
        "at": datetime.now(UTC).isoformat(),
        "kind": "session_compaction",
        "lever": "session_compaction",
        "session_id": led.session_id,
        "agent": led.agent or _detect_agent(),
        "model": model,
        "trigger": trigger,
        "reason": reason,
        "tokens_saved": tokens_freed,
        "tokens_freed": tokens_freed,
        "cost_saved_usd": cost_saved_usd,
        "tokens_before": max(0, int(tokens_before)),
        "tokens_after_estimate": tokens_after_estimate,
        "utilisation_pct": utilisation,
    }


def _emit_model_recommendation(tool_name: str, args: dict[str, Any], led: RunLedger) -> dict[str, Any]:
    payload, session_state, workflow, current_step = _prepare_model_recommendation(tool_name, args, led)
    return _finalize_model_recommendation(
        payload,
        led=led,
        tool_name=tool_name,
        session_state=session_state,
        workflow=workflow,
        current_step=current_step,
    )


def _tool_lifecycle_hooks() -> ToolLifecycleHooks:
    return ToolLifecycleHooks(
        get_ledger=_get_ledger,
        make_outcome_writer=_make_outcome_writer,
        append_live_savings_event=_append_live_savings_event,
        append_debug_event=_append_mcp_debug_event,
        append_tool_profile=_append_tool_profile,
        scrub_args_for_debug=_scrub_args_for_debug,
        process_tool_accounting=_process_tool_accounting,
        record_context_budget=_record_context_budget_for_tool,
        loop_review_enabled=_loop_review_enabled,
        loop_nudge_for_call=_loop_nudge_for_call,
        write_statusline_sidecar=_write_statusline_sidecar,
        debug_enabled=_mcp_debug_enabled,
        append_workspace_savings=_append_workspace_savings,
        spill=_spill_oversized_result_text,
        root=_lemoncrow_root,
        coerce_saved_tokens=_coerce_saved_tokens,
        extract_tokens_saved=_extract_tokens_saved,
        renderer=render_tool_result_text,
        read_tools=_READ_TOOLS,
    )


def _finalize_mcp_error(lifecycle: _ToolCallLifecycle, exc: Exception) -> dict[str, Any]:
    lifecycle.record_error(exc)
    return tool_exception_response(lifecycle.rid, exc)


def _finalize_mcp_response(lifecycle: _ToolCallLifecycle, result: Any) -> dict[str, Any]:
    try:
        return _ok(lifecycle.rid, lifecycle.finalize_payload(result))
    except Exception as exc:
        return _finalize_mcp_error(lifecycle, exc)


def _tool_call_runtime_hooks() -> ToolCallRuntimeHooks:
    return ToolCallRuntimeHooks(
        broker_spec=_TOOL_BROKER_SPEC,
        remote_tools=_REMOTE_TOOLS,
        dispatch_remote=_dispatch_remote,
        get_ledger=_get_ledger,
        prepare_model_recommendation=_prepare_model_recommendation,
        finalize_model_recommendation=_finalize_model_recommendation,
        lifecycle_hooks=_tool_lifecycle_hooks,
        record_session_cwd=_record_session_cwd,
        is_deferred_result=lambda value: isinstance(value, _DeferredResult),
    )


configure_default_tool_runtime(_tool_call_runtime_hooks)


def _run_tool_call(rid: Any, params: Any, *, payload_only: bool) -> dict[str, Any] | _Deferred:
    try:
        run = _canonical_run_tool_call(rid, params, hooks=_tool_call_runtime_hooks())
    except ToolProtocolError as exc:
        if payload_only:
            raise
        return _err(rid, exc.code, str(exc))

    lifecycle = run.lifecycle
    if payload_only:
        return finalize_tool_payload(run)
    if run.error is not None:
        return _finalize_mcp_error(lifecycle, run.error)
    if run.deferred:
        result = run.result
        assert isinstance(result, _DeferredResult)
        return _Deferred(
            src=result,
            finalize=lambda value: _finalize_mcp_response(lifecycle, value),
            finalize_error=lambda exc: _finalize_mcp_error(lifecycle, exc),
        )
    return _finalize_mcp_response(lifecycle, run.result)


def _handle_tool_payload(rid: Any, params: Any) -> dict[str, Any]:
    """Compatibility wrapper over the configured transport-neutral runtime."""
    return execute_default_tool_payload(rid, params)


def _handle_tool_call(rid: Any, params: Any) -> dict[str, Any] | _Deferred:
    return _run_tool_call(rid, params, payload_only=False)


def _handle(request: dict[str, Any]) -> dict[str, Any] | _Deferred | None:
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    control = handle_control_request(
        method,
        rid,
        on_session_start=_emit_mcp_session_start,
        visibility=_tool_visible_to_llm,
        description=_tool_description,
    )
    if control.handled:
        return control.response

    if method == "tools/call":
        return _handle_tool_call(rid, params)

    return _err(rid, -32601, f"unknown method: {method}")


# Result normalization and JSON-RPC envelope helpers live in
# ``gateway.tools.results`` and ``gateway.adapters.mcp.jsonrpc``; compatibility
# aliases are imported above.


def _mcp_max_workers() -> int:
    raw = os.environ.get("LEMONCROW_MCP_MAX_WORKERS", str(_DEFAULT_MCP_MAX_WORKERS))
    try:
        configured = int(raw)
    except ValueError:
        _log.warning(
            "invalid LEMONCROW_MCP_MAX_WORKERS=%r; using %d",
            raw,
            _DEFAULT_MCP_MAX_WORKERS,
        )
        return _DEFAULT_MCP_MAX_WORKERS
    return max(1, min(configured, _MAX_MCP_MAX_WORKERS))


_DEFAULT_MCP_HEAVY_WORKERS = 6
_MAX_MCP_HEAVY_WORKERS = 32
# Tools that can run for a long time (subprocess, network, mypy/pytest verify, or
# a workflow/agent spawn up to the 48h ceiling). They get a separate small
# executor lane so a burst can't evict cheap, frequent reads/searches from the
# main pool.
# code_search source shaping. UNCONDITIONAL: any section past _CODESEARCH_TOP2_MAX_CHARS
# keeps its head plus a precise L<start>-L<end> pointer -- one un-capped section measured
# 16.5k chars (~7.6k tokens) resident for 40+ turns. DEFAULT-ON (LEMONCROW_CODESEARCH_OUTLINE=0
# disables): large blocks collapse to a pointer-only outline -- code_search LOCATES the exact
# range to `read`; small matches stay inline (cheap, usually all the agent needs); the hidden
# include_source arg keeps bounded source for the top-2 matches.
# candidate_files/related_symbols (already pointers) stay.
_CODESEARCH_OUTLINE = os.environ.get("LEMONCROW_CODESEARCH_OUTLINE", "1").strip().lower() in {"1", "true", "on", "yes"}
# Matches at or below this many chars keep their source inline; larger ones are outlined.
_CODESEARCH_OUTLINE_MAX_CHARS = int(os.environ.get("LEMONCROW_CODESEARCH_OUTLINE_MAX_CHARS") or 400)
# include_source keeps the top-2 matches' source even in outline mode -- but bounded:
# one un-capped keep measured 16.5k chars (~7.6k tokens) resident for 40+ turns of a
# single benchmark run. Past the cap the section keeps its head plus a precise pointer.
_CODESEARCH_TOP2_MAX_CHARS = int(os.environ.get("LEMONCROW_CODESEARCH_TOP2_MAX_CHARS") or 8000)


def _outline_lean_view(lean: dict[str, Any], *, keep_top2: bool) -> dict[str, Any]:
    """Bound code_search section source; optionally outline large sections.

    Dict-level (applied in tool_code_search before rendering). ALWAYS: a section
    past _CODESEARCH_TOP2_MAX_CHARS keeps its head plus a precise L<start>-L<end>
    pointer.

    top2 (a confident call's top-ranked 1-2 sections; `files` is already
    relevance-ordered) keeps keep_top2's existing width and gate -- exact_match
    or the hidden include_source arg. A low-confidence "ranked candidates" call
    (keep_top2=False) instead protects only its SINGLE top-ranked section --
    a genuine top pick is still worth showing even on a gamble, but a
    low-confidence match ranked 2nd or later no longer earns a free pass just
    for being short: the outline collapse below applies to it UNCONDITIONALLY
    regardless of size, closing a gap where size alone (never a confidence
    signal) decided whether a low-rank guess shipped its source for free.
    """
    files = lean.get("files")
    if not isinstance(files, list):
        return lean
    match = 0  # rank across all matched sections (files are relevance-ordered)
    top_n = 2 if keep_top2 else 1
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", "")
        for sec in entry.get("sections") or []:
            if not (isinstance(sec, dict) and "content" in sec):
                continue
            top2 = match < top_n
            match += 1
            start, end = sec.get("line"), sec.get("end_line")
            # Outline mode first: a large non-top2 section always collapses to a
            # pointer-only outline (cheaper than any head cut); a small one only
            # collapses when the call isn't confident enough to earn the free pass.
            if (
                _CODESEARCH_OUTLINE
                and not top2
                and (not keep_top2 or len(sec["content"]) > _CODESEARCH_OUTLINE_MAX_CHARS)
            ):
                sym = sec.get("qualified_name") or path or "symbol"
                sec.pop("content", None)
                sec["outline"] = f"{sym} — read {path or sec.get('path', '')}:L{start}-L{end}"
                continue
            if len(sec["content"]) > _CODESEARCH_TOP2_MAX_CHARS:
                # Pathological single section: keep the head, point at the rest.
                sec["content"] = (
                    sec["content"][:_CODESEARCH_TOP2_MAX_CHARS]
                    + f"\n… [truncated — read {path or sec.get('path', '')}:L{start}-L{end}]"
                )
    return lean


_HEAVY_TOOLS = frozenset({"bash", "run", "edit", "web_fetch", "workflow", "agent"})

# Cost classes for per-request executor routing. Plain module-level str
# constants (not an Enum) for mypyc friendliness.
_COST_CPU = "cpu"  # GIL-bound, fast Python handlers: reads, searches, context,
#                    smart_read, trace, memory recall, protocol methods.
_COST_IO = "io"  # blocks on an external subprocess/socket/LLM: bash foreground,
#                  edit-with-verify, web_fetch, memory store_fact.
_COST_DETACHED = "detached"  # long-lived supervised child: workflow, agent,
#                              background bash.


def _mcp_heavy_max_workers() -> int:
    raw = os.environ.get("LEMONCROW_MCP_HEAVY_WORKERS", str(_DEFAULT_MCP_HEAVY_WORKERS))
    try:
        configured = int(raw)
    except ValueError:
        return _DEFAULT_MCP_HEAVY_WORKERS
    return max(1, min(configured, _MAX_MCP_HEAVY_WORKERS))


_DEFAULT_MCP_IO_WORKERS = 32
_MAX_MCP_IO_WORKERS = 128


def _mcp_io_max_workers() -> int:
    """Worker count for the IO lane (subprocess/socket/LLM-blocking tools).

    Reads LEMONCROW_MCP_IO_WORKERS (default 32, max 128). For back-compat, if the
    old heavy knob LEMONCROW_MCP_HEAVY_WORKERS is set and LEMONCROW_MCP_IO_WORKERS is
    not, the heavy value is used for the IO lane.
    """
    raw = os.environ.get("LEMONCROW_MCP_IO_WORKERS")
    if raw is None:
        if os.environ.get("LEMONCROW_MCP_HEAVY_WORKERS") is not None:
            return _mcp_heavy_max_workers()
        raw = str(_DEFAULT_MCP_IO_WORKERS)
    try:
        configured = int(raw)
    except ValueError:
        _log.warning(
            "invalid LEMONCROW_MCP_IO_WORKERS=%r; using %d",
            raw,
            _DEFAULT_MCP_IO_WORKERS,
        )
        return _DEFAULT_MCP_IO_WORKERS
    return max(1, min(configured, _MAX_MCP_IO_WORKERS))


def _classify_cost(req: dict[str, Any]) -> str:
    """Classify a JSON-RPC request into a cost class for executor routing.

    Returns one of _COST_CPU, _COST_IO, _COST_DETACHED.
    """
    if req.get("method") != "tools/call":
        # Protocol methods are trivial; the cheap CPU lane handles them.
        return _COST_CPU
    params = req.get("params") or {}
    if not isinstance(params, dict):
        return _COST_CPU
    name = params.get("name")
    if name == "run":
        name = "bash"
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}
    if name in {"workflow", "agent"}:
        return _COST_DETACHED
    if name == "bash":
        backgrounded = args.get("background") is True
        if not backgrounded:
            command = args.get("command")
            if isinstance(command, str):
                stripped = command.rstrip()
                backgrounded = stripped.endswith("&") and not stripped.endswith("&&")
        return _COST_DETACHED if backgrounded else _COST_IO
    if name in {"edit", "web_fetch"}:
        return _COST_IO
    # memory store_fact runs a blocking arbiter LLM call.
    if name == "memory" and args.get("op") == "store_fact":
        return _COST_IO
    return _COST_CPU


def _is_heavy_request(req: dict[str, Any]) -> bool:
    """True if this JSON-RPC request is not a cheap CPU-lane request.

    Retained for back-compat with existing callers/tests; the live routing
    decision in serve() is made by _classify_cost.
    """
    return _classify_cost(req) != _COST_CPU


def _apply_batch_read_budget(
    results: list[dict[str, Any]],
    entry_specs: list[str | None],
    include_meta: bool,
) -> list[str]:
    """Compatibility wrapper using the live MCP smart-read implementation."""
    return _canonical_apply_batch_read_budget(
        results,
        entry_specs,
        include_meta,
        outline_reader=_smart_read_single,
    )


def _write_jsonrpc(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n"
    # Hard backstop: a frame above the host's ~16 MiB stdout guard disconnects
    # the server. Per-result capping should prevent this, but escaping overhead
    # or non-result frames could still exceed it — replace such a frame with
    # a small error so the session survives instead of dropping.
    if len(payload.encode("utf-8")) > _MAX_WIRE_BYTES and message.get("id") is not None:
        _log.warning(
            "jsonrpc frame exceeds %d bytes; replacing with error to protect the connection",
            _MAX_WIRE_BYTES,
        )
        message = _err(
            message["id"],
            -32000,
            f"result exceeded the {_MAX_WIRE_BYTES} byte MCP frame limit and was dropped to "
            "keep the connection alive; re-request a narrower slice.",
        )
        payload = json.dumps(message, ensure_ascii=False) + "\n"
    with _STDOUT_LOCK:
        sys.stdout.write(payload)
        sys.stdout.flush()


def _handle_and_write(request: dict[str, Any]) -> None:
    # Mark this worker thread as deferral-capable for the duration of _handle so a
    # foreground bash command can hand the worker back and let the watcher finalize
    # the response (see _deferral_supported). Reset in finally so a pooled worker
    # never carries the flag into non-tool work.
    _deferral_context.active = True
    try:
        response = _handle(request)
        if isinstance(response, _Deferred):
            deferred = response

            def _on_complete() -> None:
                try:
                    concrete = deferred.src.collect()
                except Exception as exc:
                    # A failed deferred result (e.g. web_fetch network/SSRF error)
                    # goes through the same tool-error pipeline as the sync path,
                    # for a byte-identical error response.
                    try:
                        resp = deferred.finalize_error(exc)
                    except Exception:
                        _log.exception("deferred MCP error-finalize failed")
                        resp = _err(request.get("id"), -32603, f"internal error: {exc}")
                else:
                    try:
                        resp = deferred.finalize(concrete)
                    except Exception as exc:
                        _log.exception("deferred MCP continuation failed")
                        resp = _err(request.get("id"), -32603, f"internal error: {exc}")
                with contextlib.suppress(Exception):
                    _write_jsonrpc(resp)

            armed = deferred.src.register(_on_complete)
            if armed is False:
                # The work already finished before we could arm the watcher;
                # produce the response now on this worker thread.
                _on_complete()
            return
    except Exception as exc:
        _log.exception("unhandled MCP request failure")
        response = _err(request.get("id"), -32603, f"internal error: {exc}")
    finally:
        _deferral_context.active = False
    if response is not None:
        try:
            _write_jsonrpc(response)
        except OSError:
            _log.exception("failed to write MCP response (connection closed)")
        except Exception:
            _log.exception("failed to write MCP response (unexpected error)")


def serve() -> None:
    # CPU lane: GIL-bound, fast Python handlers (reads, searches, context,
    # smart_read, trace, memory recall, protocol methods). This is the old
    # "light" pool, unchanged in size.
    cpu_executor = ThreadPoolExecutor(
        max_workers=_mcp_max_workers(),
        thread_name_prefix="lemoncrow-cpu",
    )
    # IO lane: tools that block on an external subprocess/socket/LLM
    # (bash foreground, edit-with-verify, web_fetch, memory store_fact).
    # NOTE(phase-1): detached-class requests (workflow/agent/background bash) are
    # routed to this IO lane for now; a later phase will give the detached class
    # true no-slot handling.
    io_executor = ThreadPoolExecutor(
        max_workers=_mcp_io_max_workers(),
        thread_name_prefix="lemoncrow-io",
    )

    def _stdin_reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except (json.JSONDecodeError, RecursionError) as exc:
                # RecursionError (deeply-nested JSON, e.g. a line of many '[')
                # is NOT a JSONDecodeError; without it here one adversarial line
                # kills the reader thread and tears down the whole stdio server.
                _write_jsonrpc(_err(None, -32700, f"parse error: {type(exc).__name__}"))
                continue
            # Valid JSON but not a request object (batch array, string, null):
            # answer -32600 instead of letting .get() kill the reader thread
            # (mirrors the mcp_http.py transport guard).
            if not isinstance(req, dict):
                _write_jsonrpc(_err(None, -32600, "invalid request: expected a JSON object"))
                continue
            # Initialization establishes client capabilities and must complete
            # before later requests can observe them.
            if req.get("method") in {"initialize", "notifications/initialized"}:
                _handle_and_write(req)
                continue
            executor = io_executor if _classify_cost(req) != _COST_CPU else cpu_executor
            executor.submit(_handle_and_write, req)

    reader = threading.Thread(target=_stdin_reader, daemon=True, name="mcp-stdin-reader")
    reader.start()
    try:
        reader.join()
    finally:
        cpu_executor.shutdown(wait=True, cancel_futures=False)
        io_executor.shutdown(wait=True, cancel_futures=False)
        _emit_mcp_session_end()
        from lemoncrow.core.service.telemetry import shutdown_otel

        shutdown_otel()
        with contextlib.suppress(Exception):
            from lemoncrow.gateway.integrations.langfuse import shutdown as _lf_shutdown

            _lf_shutdown()


# Rotate mcp.log at 10MB, keeping 5 backups (~60MB ceiling) so a long-lived
# daemon's log never grows unbounded.
_MCP_LOG_MAX_BYTES = 10 * 1024 * 1024
_MCP_LOG_BACKUP_COUNT = 5


def _setup_file_logging(root: str | Path) -> None:
    """Configure the lemoncrow.mcp logger to write to a rotating file.

    This ensures logs survive process termination and can be inspected
    via ``lc logs mcp``. Rotates at ``_MCP_LOG_MAX_BYTES`` so a long-lived
    daemon never accumulates an unbounded log.
    """
    from logging.handlers import RotatingFileHandler

    log_dir = Path(root) / "mcp"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "mcp.log"

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=_MCP_LOG_MAX_BYTES,
        backupCount=_MCP_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    mcp_logger = logging.getLogger("lemoncrow.mcp")
    mcp_logger.addHandler(handler)
    mcp_logger.setLevel(logging.DEBUG)


def _auto_init_workspace() -> None:
    """One-time workspace bootstrap: seed playbooks, add .gitignore, write marker.

    Runs as a daemon thread on stdio MCP startup.  Fail-open so a crash never
    blocks server startup.  Idempotent via ``.workspace_inited`` marker file.
    """
    try:
        from importlib import resources as _resources

        import yaml

        from lemoncrow.core.foundation.models import Playbook, Rubric
        from lemoncrow.core.foundation.paths import (
            ensure_gitignore,
            is_recognized_workspace,
            resolve_workspace_store_dir,
        )
        from lemoncrow.infra.storage.factory import create_store

        lemoncrow_root = _lemoncrow_root()
        ws_root = _workspace_root()

        if not is_recognized_workspace(ws_root):
            # ws_root falls all the way back to cwd when no host set a
            # workspace env var and no git repo was detected -- never
            # auto-create `.lemoncrow/` (with seeded lessons/rubrics) at an
            # arbitrary directory like $HOME or a multi-repo container. Only
            # a git repo or an already `lc init`-registered dir qualifies.
            _log.debug("skipping workspace auto-init: %s is not a recognized workspace", ws_root)
            return

        marker = resolve_workspace_store_dir(lemoncrow_root, ws_root) / ".workspace_inited"
        if marker.exists():
            return

        # --- Seed playbooks and rubrics ---
        store = create_store(lemoncrow_root)
        store.init()

        blocks_dir = _resources.files("lemoncrow") / "infra" / "seed_playbooks"
        rubric_dir = _resources.files("lemoncrow") / "core" / "rubrics"

        for path in sorted(Path(str(p)) for p in blocks_dir.iterdir() if p.name.endswith(".yaml")):
            data = yaml.safe_load(path.read_text("utf-8"))
            if not isinstance(data, dict):
                continue
            if "id" not in data:
                try:
                    data["id"] = Playbook.make_id(data.get("title", ""), data.get("domain", ""))
                except (KeyError, ValueError):
                    continue
            try:
                store.knowledge.upsert_block(Playbook.model_validate(data))
            except (KeyError, ValueError):
                continue

        for path in sorted(Path(str(p)) for p in rubric_dir.iterdir() if p.name.endswith(".yaml")):
            data = yaml.safe_load(path.read_text("utf-8"))
            if not isinstance(data, dict):
                continue
            try:
                store.knowledge.upsert_rubric(Rubric.model_validate(data))
            except (KeyError, ValueError):
                continue

        # --- Add .gitignore ---
        ensure_gitignore(ws_root)

        # --- Write marker ---
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
    except Exception:
        logging.exception("auto-init failed, continuing")


def _warm_stdio_code_index() -> None:
    """Warm the single-workspace code-context engine for the stdio MCP path.

    Reuses the service ``_CodeWarmer`` patterns via ``warm_stdio_workspace``.
    Fail-open: any failure is swallowed so stdio server startup is unaffected.

    No-ops when ``_workspace_root()`` isn't a git repo or an ``lc
    init``-registered directory: ``warm_stdio_workspace`` has no such check
    of its own and will fire an ``lc code index`` subprocess against
    *any* directory (e.g. cwd falling all the way through to an unrelated
    multi-repo container or ``$HOME``), which never finishes and burns a CPU
    core indefinitely.
    """
    from lemoncrow.core.foundation.paths import is_recognized_workspace

    ws_root = _workspace_root()
    if not is_recognized_workspace(ws_root):
        _log.debug("skipping stdio code-index warm: %s is not a recognized workspace", ws_root)
        return
    try:
        from lemoncrow.core.service.code_warm import warm_stdio_workspace

        warm_stdio_workspace(ws_root)
    except Exception:
        logging.exception("Recovered from broad exception handler")
    # Warm the query path of the cached serving engine (page cache, centrality,
    # ANN matrix) so the first tool call never pays a cold-DB spike. Runs on
    # this daemon thread; fail-open.
    try:
        engine = _code_context_engine(str(ws_root))
        _log.info("code query path warmed: %s", engine.warm_query_path())
    except Exception:
        _log.debug("query-path warm failed", exc_info=True)


def _warm_stdio_embedder() -> None:
    """Pre-load the configured embedder so the first semantic query is instant.

    No-op when the default NullEmbedder is active (embedding off by default).
    Only fires when LEMONCROW_CODE_EMBEDDER is set to bge/ollama/openai.
    Fail-open: any failure is logged and ignored.
    """
    try:
        from lemoncrow.infra.embeddings.factory import get_code_embedder

        embedder = get_code_embedder()
        if callable(getattr(embedder, "_load", None)):
            embedder._load()  # type: ignore[attr-defined]
            _log.info("Embedder pre-warmed: %s dim=%s", getattr(embedder, "name", "?"), getattr(embedder, "dim", "?"))
    except Exception:
        _log.debug("Embedder pre-warm failed", exc_info=True)


def _warm_stdio_zoekt_webserver() -> None:
    """Start the zoekt-webserver eagerly for the stdio workspace.

    The persistent ``zoekt-webserver`` keeps the index resident in memory so
    every subsequent query is single-digit-ms.  Starting it here on a daemon
    thread at MCP startup amortises the ~1-2 s shard-load cost over the entire
    session instead of charging it to the first search query.

    Lifecycle: the webserver subprocess is owned by the ``ZoektServer`` instance
    cached in ``get_zoekt_server()``.  ``atexit`` ensures ``server.stop()`` is
    called on clean MCP exit so the child process is reaped properly.  If the
    MCP process is killed or crashes, ``prctl(PR_SET_PDEATHSIG)`` in the child
    ensures the kernel delivers ``SIGTERM`` to the webserver subprocess.

    Fail-open: any error (no index built yet, binary missing) is logged at
    DEBUG and the fallback per-query CLI path remains active.
    """
    try:
        from lemoncrow.infra.code_intel.zoekt.binary import discover_zoekt_binary
        from lemoncrow.infra.code_intel.zoekt.server import get_zoekt_server

        ws = Path(_workspace_root())
        resolution = discover_zoekt_binary(ws)
        if not resolution.available:
            return
        server = get_zoekt_server(ws, resolution=resolution)
        # ensure_started_and_build() registers the binary handle and builds the
        # index if it is missing (e.g. a fresh swarm worktree). Runs on a daemon
        # thread so the build doesn't block the first MCP response.
        server.ensure_started_and_build()
        # _ensure_webserver() starts the persistent HTTP server and waits until
        # the index shards are loaded and queryable (per /api/list readiness).
        url = server._ensure_webserver()
        if url:
            _log.info("zoekt webserver ready at %s", url)
            import atexit

            atexit.register(server.stop)
        else:
            _log.debug("zoekt webserver did not start; using CLI fallback")
    except Exception:
        _log.debug("zoekt pre-warm failed", exc_info=True)


def _shutdown_managed_bash_commands() -> None:
    from lemoncrow.pro.capabilities.tool_supervision.bash_exec import cleanup_managed_commands

    summary = cleanup_managed_commands()
    terminated = summary["terminated"]
    preserved = summary["preserved"]
    if terminated:
        logger.warning(
            "MCP shutdown terminated %d foreground Bash command(s): %s",
            len(terminated),
            ", ".join(str(row["session_id"]) for row in terminated),
        )
    if preserved:
        logger.info(
            "MCP shutdown preserved %d explicit bg=true Bash command(s): %s",
            len(preserved),
            ", ".join(str(row["session_id"]) for row in preserved),
        )


def main() -> None:
    # Legacy direct stdio entrypoint. Remote service routing remains explicit:
    # only an exported LEMONCROW_SERVICE_URL may enable it.
    # The host's own per-window folder wins; only when it says nothing do we
    # detect the git repo root, so global-mode installs on any host still point
    # at a project root rather than the editor's cwd.
    from lemoncrow.core.foundation.paths import host_workspace_root as _host_workspace_root

    _declared_workspace = _host_workspace_root()
    if _declared_workspace is not None:
        os.environ["LEMONCROW_WORKSPACE_ROOT"] = str(_declared_workspace)
    else:
        try:
            import subprocess as _subprocess

            _git_result = _subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if _git_result.returncode == 0:
                os.environ["LEMONCROW_WORKSPACE_ROOT"] = _git_result.stdout.strip()
        except (OSError, _subprocess.SubprocessError):
            _log.debug("git rev-parse workspace-root detection failed", exc_info=True)
    os.environ.setdefault("LEMONCROW_WORKSPACE_ROOT", os.getcwd())
    os.environ.setdefault(
        "LEMONCROW_LESSONS_ROOT", os.path.join(os.environ["LEMONCROW_WORKSPACE_ROOT"], ".lemoncrow/lessons")
    )

    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        sys.stdout.write(f"lc mcp {SERVER_VERSION}\n")
        return
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            os.environ["LEMONCROW_ROOT"] = argv[i + 1]
    if "--host" in argv:
        i = argv.index("--host")
        if i + 1 < len(argv):
            os.environ["LEMONCROW_AGENT"] = argv[i + 1]

    # Set up file-based logging so logs survive process termination.
    lemoncrow_root = os.environ.get("LEMONCROW_ROOT", str(Path.home() / ".lemoncrow"))
    _setup_file_logging(lemoncrow_root)

    # Register before serve() so the SessionStart hook can find this process
    # and write the Claude session UUID before the first tool call arrives.
    _register_mcp_session()

    # Warm the code-context engine/index once on stdio startup (G10) so the
    # first code-context tool call does not pay cold-start on Zoekt/ast-grep subprocesses. Off the hot path in a daemon thread; fail-open so
    # warming failure never breaks server startup.
    threading.Thread(target=_warm_stdio_code_index, daemon=True).start()
    # Pre-load embedder if explicitly configured (no-op with default NullEmbedder).
    threading.Thread(target=_warm_stdio_embedder, daemon=True).start()
    # Eagerly start the zoekt-webserver so the resident index is queryable from
    # the first explore call.  Lifecycle (atexit stop) wired inside the fn.
    threading.Thread(target=_warm_stdio_zoekt_webserver, daemon=True).start()

    # One-time workspace bootstrap: seed playbooks, add .lemoncrow/.gitignore,
    # and write the .workspace_inited marker.  Daemon thread, fail-open.
    threading.Thread(target=_auto_init_workspace, daemon=True).start()

    _update_thread = threading.Thread(target=_check_auto_update, daemon=True)
    _update_thread.start()
    # Convert process-termination signals into normal Python unwinding so the
    # finally block can terminate every MCP-owned process group. SIGKILL remains
    # uncatchable by definition.
    import signal as _signal

    previous_handlers: list[tuple[_signal.Signals, Any]] = []
    if threading.current_thread() is threading.main_thread():

        def _exit_on_signal(signum: int, _frame: Any) -> None:
            raise SystemExit(128 + signum)

        for signum in (_signal.SIGTERM, _signal.SIGHUP):
            previous_handlers.append((_signal.Signals(signum), _signal.getsignal(signum)))
            _signal.signal(signum, _exit_on_signal)
    try:
        serve()
    finally:
        _shutdown_managed_bash_commands()
        _unregister_mcp_session()
        # If an opt-in auto-update reinstall (git pull + install.sh) is mid-flight
        # when the host disconnects, let it finish rather than killing the daemon
        # thread abruptly and leaving a half-pulled tree / partial install. Returns
        # immediately in the common no-update case (the thread already exited);
        # only blocks when an install is genuinely running (its own cap is ~300s).
        _update_thread.join(timeout=310.0)
        for signum, handler in previous_handlers:
            _signal.signal(signum, handler)
