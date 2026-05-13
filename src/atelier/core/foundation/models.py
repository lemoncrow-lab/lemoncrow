"""Pydantic data models for the reasoning runtime.

These types are the contract between every layer (store, retriever, plan
checker, rubric gate, CLI, MCP). Field names are kept stable and explicit
so traces and ReasonBlocks remain forward-compatible.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

BlockStatus = Literal["active", "deprecated", "quarantined"]
TraceStatus = Literal["success", "failed", "partial"]
TraceConfidence = Literal["full_live", "mcp_live", "wrapper_live", "imported", "manual"]
PlanStatus = Literal["pass", "warn", "blocked"]
Severity = Literal["low", "medium", "high"]
ConsolidationKind = Literal["duplicate_cluster", "stale_candidate", "low_confidence"]
ConsolidationAction = Literal["merge", "deprecate", "delete"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def slugify(text: str) -> str:
    """Lowercase, dash-separated slug. Used for stable block/rubric IDs."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "untitled"


def short_hash(text: str, length: int = 8) -> str:
    """Stable short hex hash, used as a fallback ID component."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


# --------------------------------------------------------------------------- #
# ReasonBlock                                                                 #
# --------------------------------------------------------------------------- #


class ReasonBlock(BaseModel):
    """A reusable engineering / product procedure.

    A ReasonBlock is **not** memory and **not** hidden chain-of-thought.
    It is an explicit, reviewable procedure that any agent can read.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    domain: str
    task_types: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    file_patterns: list[str] = Field(default_factory=list)
    tool_patterns: list[str] = Field(default_factory=list)

    situation: str
    dead_ends: list[str] = Field(default_factory=list)
    procedure: list[str]
    verification: list[str] = Field(default_factory=list)
    failure_signals: list[str] = Field(default_factory=list)
    required_rubrics: list[str] = Field(default_factory=list)
    when_not_to_apply: str = ""

    status: BlockStatus = "active"
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("procedure")
    @classmethod
    def _procedure_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("procedure must contain at least one step")
        return v

    @classmethod
    def make_id(cls, title: str, domain: str) -> str:
        base = slugify(f"{domain}-{title}")
        return f"{base}-{short_hash(base, 6)}"

    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 0.0


# --------------------------------------------------------------------------- #
# Trace                                                                       #
# --------------------------------------------------------------------------- #


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args_hash: str
    count: int = 1
    args: dict[str, Any] | None = None
    result_summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class CommandRecord(BaseModel):
    """A recorded shell command with its output."""

    model_config = ConfigDict(extra="forbid")
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class FileEditRecord(BaseModel):
    """A recorded file edit with its diff."""

    model_config = ConfigDict(extra="forbid")
    path: str
    diff: str = ""
    event: str = "edit"  # "edit" | "create" | "delete" | "revert"


class RepeatedFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signature: str
    count: int
    last_seen_at: datetime = Field(default_factory=_utcnow)


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    detail: str = ""


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class Trace(BaseModel):
    """An observable record of an agent run.

    Stores only what is observable: files, commands, errors, validation
    results, and reasoning/thinking snippets when available from the source.
    Hidden chain-of-thought is preserved in raw artifacts for audit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str | None = None
    agent: str
    domain: str
    task: str
    status: TraceStatus
    files_touched: list[str | FileEditRecord] = Field(default_factory=list)
    tools_called: list[ToolCall] = Field(default_factory=list)
    commands_run: list[str | CommandRecord] = Field(default_factory=list)
    errors_seen: list[str] = Field(default_factory=list)
    repeated_failures: list[RepeatedFailure] = Field(default_factory=list)
    diff_summary: str = ""
    output_summary: str = ""
    validation_results: list[ValidationResult] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)
    raw_artifact_ids: list[str] = Field(default_factory=list)
    host: str | None = None
    trace_confidence: TraceConfidence | None = None
    capture_sources: list[str] = Field(default_factory=list)
    missing_surfaces: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    user_prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model: str = ""
    model_usages: list[ModelUsage] = Field(default_factory=list)
    workspace_path: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    snippets: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _consolidate_session_id(cls, data: Any) -> Any:
        if isinstance(data, dict):
            legacy_run_id = data.pop("run_id", None)
            session_id = data.pop("session_id", None)
            if not session_id:
                session_id = legacy_run_id
            if session_id and not data.get("session_id"):
                data["session_id"] = session_id
        return data

    @classmethod
    def make_id(cls, task: str, agent: str, created_at: datetime | None = None) -> str:
        ts = (created_at or _utcnow()).strftime("%Y%m%dT%H%M%S")
        return f"{ts}-{slugify(agent)}-{short_hash(task, 8)}"


# --------------------------------------------------------------------------- #
# Raw artifacts                                                               #
# --------------------------------------------------------------------------- #


class RawArtifact(BaseModel):
    """Redacted source material linked to a curated trace.

    Traces stay compact and retrieval-friendly. RawArtifact keeps the fuller
    source data available for audit/debug lookup without storing unredacted
    secrets or hidden reasoning in the trace itself.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    source_session_id: str
    kind: str
    relative_path: str
    content_path: str
    sha256_original: str
    sha256_redacted: str
    byte_count_original: int
    byte_count_redacted: int
    redacted: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    source_file_mtime: datetime | None = None  # filesystem mtime when imported
    source_path: str | None = None  # original filesystem path of the imported file


# --------------------------------------------------------------------------- #
# Rubric                                                                      #
# --------------------------------------------------------------------------- #


class Rubric(BaseModel):
    """A domain-specific verification rubric.

    Defines the explicit checks that an agent's output must satisfy
    before being accepted.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    domain: str
    triggers: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    block_if_missing: list[str] = Field(default_factory=list)
    warning_checks: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)
    related_blocks: list[str] = Field(default_factory=list)


class ConsolidationCandidate(BaseModel):
    """Human-reviewed proposal for consolidating stale or duplicate knowledge rows."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"cc-{short_hash(str(_utcnow().timestamp()), 12)}")
    kind: ConsolidationKind
    affected_block_ids: list[str] = Field(default_factory=list)
    proposed_action: ConsolidationAction
    proposed_body: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision: str | None = None


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


class PlanWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Severity
    reason_block: str
    message: str


class PlanCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: PlanStatus
    warnings: list[PlanWarning] = Field(default_factory=list)
    suggested_plan: list[str] = Field(default_factory=list)
    matched_blocks: list[str] = Field(default_factory=list)


class RescueResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rescue: str
    matched_blocks: list[str] = Field(default_factory=list)


class RubricCheckOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["pass", "fail", "missing", "warn"]
    detail: str = ""


class RubricResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rubric_id: str
    status: Literal["pass", "warn", "blocked", "escalate"]
    outcomes: list[RubricCheckOutcome] = Field(default_factory=list)
    escalations: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Convenience: dict <-> model conversions for storage                         #
# --------------------------------------------------------------------------- #


def to_jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


# Known fields removed from the Trace model but possibly present in old stored payloads
_LEGACY_TRACE_FIELDS: frozenset[str] = frozenset({"run_id"})


def coerce_trace_json(payload: str) -> str:
    """Strip known legacy fields from a stored trace JSON payload before model validation.

    Allows old payloads (e.g. with *run_id*) to be read without crashing even
    after the field was removed from the ``Trace`` model.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    changed = False
    for field in _LEGACY_TRACE_FIELDS:
        if field in data:
            del data[field]
            changed = True
    if not changed:
        return payload
    return json.dumps(data, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# V2: status widening                                                         #
# --------------------------------------------------------------------------- #

RubricStatus = Literal["pass", "warn", "blocked", "escalate"]


# --------------------------------------------------------------------------- #
# V2: Run ledger event                                                        #
# --------------------------------------------------------------------------- #


class LedgerEvent(BaseModel):
    """A single observable event during an agent run."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "tool_call",
        "tool_result",
        "command",
        "command_result",
        "file_edit",
        "file_revert",
        "watchdog_alert",
        "rubric_run",
        "validation",
        "test_result",
        "note",
        "reasoning",
        "agent_message",
    ]
    at: datetime = Field(default_factory=_utcnow)
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# V2: Failure cluster                                                         #
# --------------------------------------------------------------------------- #


class FailureCluster(BaseModel):
    """A group of failed traces that share an error fingerprint."""

    model_config = ConfigDict(extra="forbid")

    id: str
    domain: str
    fingerprint: str
    trace_ids: list[str] = Field(default_factory=list)
    sample_errors: list[str] = Field(default_factory=list)
    suggested_block_title: str = ""
    suggested_rubric_check: str = ""
    suggested_eval_case: str = ""
    suggested_prompt: str = ""
    severity: Severity = "medium"


# --------------------------------------------------------------------------- #
# V2: Eval case                                                               #
# --------------------------------------------------------------------------- #


class EvalCase(BaseModel):
    """A reusable structural evaluation case for the reasoning runtime."""

    model_config = ConfigDict(extra="forbid")

    id: str
    domain: str
    description: str
    plan: list[str]
    expected_status: PlanStatus
    expected_warnings_contain: list[str] = Field(default_factory=list)
    expected_dead_ends: list[str] = Field(default_factory=list)
