"""Product telemetry event registry and validation helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

PropertyTypes = tuple[type, ...]


@dataclass(frozen=True)
class EventSpec:
    props: dict[str, PropertyTypes]


STR: PropertyTypes = (str,)
BOOL: PropertyTypes = (bool,)
INT: PropertyTypes = (int,)
FLOAT: PropertyTypes = (float, int)


EVENTS: dict[str, EventSpec] = {
    "session_start": EventSpec(
        {
            "agent_host": STR,
            "lemoncrow_version": STR,
            "os": STR,
            "py_version": STR,
            "anon_id": STR,
            "session_id": STR,
        }
    ),
    "session_end": EventSpec({"session_id": STR, "duration_s_bucket": STR, "exit_reason": STR}),
    "session_interrupted": EventSpec({"session_id": STR, "signal": STR, "elapsed_s_bucket": STR, "last_phase": STR}),
    "cli_command_invoked": EventSpec({"command_name": STR, "session_id": STR, "anon_id": STR}),
    "cli_command_completed": EventSpec({"command_name": STR, "session_id": STR, "duration_ms_bucket": STR, "ok": BOOL}),
    "mcp_tool_called": EventSpec({"tool_name": STR, "session_id": STR, "duration_ms_bucket": STR, "ok": BOOL}),
    "api_request": EventSpec({"endpoint": STR, "method": STR, "status_code": INT, "duration_ms_bucket": STR}),
    "playbook_retrieved": EventSpec(
        {
            "block_id_hash": STR,
            "domain": STR,
            "retrieval_score": FLOAT,
            "rank": INT,
            "session_id": STR,
        }
    ),
    "playbook_applied": EventSpec({"block_id_hash": STR, "domain": STR, "retrieval_score": FLOAT, "session_id": STR}),
    "playbook_rejected": EventSpec({"block_id_hash": STR, "domain": STR, "rejection_reason": STR, "session_id": STR}),
    "plan_check_passed": EventSpec({"domain": STR, "rule_count": INT, "session_id": STR}),
    "plan_check_blocked": EventSpec({"domain": STR, "blocking_rule_id": STR, "severity": STR, "session_id": STR}),
    "plan_check_overridden": EventSpec({"domain": STR, "blocking_rule_id": STR, "session_id": STR}),
    "plan_modified_by_user": EventSpec(
        {
            "domain": STR,
            "edit_distance_bucket": STR,
            "steps_added": INT,
            "steps_removed": INT,
            "session_id": STR,
        }
    ),
    "rescue_offered": EventSpec({"cluster_id_hash": STR, "rescue_type": STR, "session_id": STR}),
    "rescue_accepted": EventSpec({"cluster_id_hash": STR, "session_id": STR}),
    "frustration_signal_behavioral": EventSpec({"signal_type": STR, "session_id": STR}),
    "frustration_signal_lexical": EventSpec({"category": STR, "surface": STR, "session_id": STR}),
    "value_estimate": EventSpec(
        {
            "session_id": STR,
            "tokens_saved_estimate": INT,
            "cache_hits": INT,
            "total_tool_calls": INT,
            "cache_hit_rate": FLOAT,
            "blocks_applied": INT,
        }
    ),
    "code_index_completed": EventSpec({"repo_id": STR, "files_indexed": INT, "symbols_indexed": INT}),
    "code_context_retrieved": EventSpec({"repo_id": STR, "operation": STR, "result_count": INT}),
    "code_symbol_retrieved": EventSpec({"repo_id": STR, "kind": STR}),
    "optimization_proposal_evaluated": EventSpec(
        {
            "source": STR,
            "repo_id": STR,
            "has_recommendation": BOOL,
            "projected_tokens_saved": INT,
            "projected_weekly_savings_usd": FLOAT,
            "benchmark_evidence_present": BOOL,
            "ni_passed": BOOL,
            "open_pr_requested": BOOL,
        }
    ),
    "optimization_proposal_result": EventSpec(
        {
            "source": STR,
            "repo_id": STR,
            "action": STR,
            "benchmark_evidence_present": BOOL,
            "ni_passed": BOOL,
            "open_pr_requested": BOOL,
        }
    ),
    "mcp_auto_update_failed": EventSpec({"current_version": STR, "session_id": STR}),
}


DURATION_MS_BUCKETS = ["<100", "100-500", "500-2000", "2000-10000", ">10000"]
DURATION_S_BUCKETS = ["<10", "10-60", "60-300", "300-1800", ">1800"]
EDIT_DISTANCE_BUCKETS = ["none", "small", "medium", "large"]

ENUMS: dict[tuple[str, str], set[str]] = {
    ("session_end", "duration_s_bucket"): set(DURATION_S_BUCKETS),
    ("session_interrupted", "elapsed_s_bucket"): set(DURATION_S_BUCKETS),
    ("cli_command_completed", "duration_ms_bucket"): set(DURATION_MS_BUCKETS),
    ("mcp_tool_called", "duration_ms_bucket"): set(DURATION_MS_BUCKETS),
    ("api_request", "duration_ms_bucket"): set(DURATION_MS_BUCKETS),
    ("plan_modified_by_user", "edit_distance_bucket"): set(EDIT_DISTANCE_BUCKETS),
    (
        "frustration_signal_behavioral",
        "signal_type",
    ): {
        "retry_burst",
        "file_revert",
        "abandon_after_error",
        "plan_resubmitted_unchanged",
        "repeated_dead_end",
    },
    ("frustration_signal_lexical", "surface"): {"cli_input", "mcp_prompt", "api_body"},
}


def bucket_duration_ms(value: float | int) -> str:
    if value < 100:
        return "<100"
    if value < 500:
        return "100-500"
    if value < 2000:
        return "500-2000"
    if value < 10000:
        return "2000-10000"
    return ">10000"


def bucket_duration_s(value: float | int) -> str:
    if value < 10:
        return "<10"
    if value < 60:
        return "10-60"
    if value < 300:
        return "60-300"
    if value < 1800:
        return "300-1800"
    return ">1800"


def bucket_edit_distance(ratio: float) -> str:
    if ratio <= 0:
        return "none"
    if ratio < 0.25:
        return "small"
    if ratio < 0.6:
        return "medium"
    return "large"


def hash_identifier(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha256:{digest}"


def validate_event_props(event: str, props: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    spec = EVENTS.get(event)
    if spec is None:
        return None, list(props)

    filtered: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in props.items():
        expected = spec.props.get(key)
        if expected is None:
            dropped.append(key)
            continue
        if _value_matches(value, expected) and _value_allowed(event, key, value):
            filtered[key] = value
        else:
            dropped.append(key)
    return filtered, dropped


def schema_dump() -> dict[str, Any]:
    return {
        "events": {
            name: {
                "props": list(spec.props),
                "example": example_payload(name),
            }
            for name, spec in EVENTS.items()
        },
        "buckets": {
            "duration_ms_bucket": DURATION_MS_BUCKETS,
            "duration_s_bucket": DURATION_S_BUCKETS,
            "elapsed_s_bucket": DURATION_S_BUCKETS,
            "edit_distance_bucket": EDIT_DISTANCE_BUCKETS,
        },
    }


def example_payload(event: str) -> dict[str, Any]:
    spec = EVENTS[event]
    return {key: _example_value(key, expected) for key, expected in spec.props.items()}


def _value_matches(value: Any, expected: PropertyTypes) -> bool:
    if expected == BOOL:
        return isinstance(value, bool)
    if expected == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _value_allowed(event: str, key: str, value: Any) -> bool:
    allowed = ENUMS.get((event, key))
    return allowed is None or value in allowed


def _example_value(key: str, expected: PropertyTypes) -> Any:
    if key.endswith("_bucket"):
        if key == "duration_ms_bucket":
            return "100-500"
        if key == "edit_distance_bucket":
            return "small"
        return "10-60"
    if key.endswith("_hash") or key in {"blocking_rule_id", "cluster_id_hash"}:
        return "sha256:0123456789abcdef"
    if key.endswith("_id") or key == "session_id":
        return "00000000-0000-4000-8000-000000000000"
    if expected == BOOL:
        return True
    if expected == INT:
        return 1
    if expected == FLOAT:
        return 0.82
    if key == "surface":
        return "cli_input"
    if key == "signal_type":
        return "retry_burst"
    return "example"
