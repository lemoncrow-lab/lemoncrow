"""V2 routing and verification models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lemoncrow.core.foundation.models import ValidationResult, _utcnow
from lemoncrow.infra.storage.ids import make_uuid7

TaskType = Literal["debug", "feature", "refactor", "test", "explain", "review", "docs", "ops"]
RiskLevel = Literal["low", "medium", "high"]
CachePolicy = Literal["prefer_cache", "neutral", "disable"]
StepType = Literal[
    "classify",
    "compress",
    "retrieve",
    "plan",
    "edit",
    "debug",
    "verify",
    "summarize",
    "lesson_extract",
]
ExecutionTier = Literal["deterministic", "cheap", "mid", "premium"]
RubricStatus = Literal["not_run", "pass", "warn", "fail"]
VerificationOutcome = Literal["pass", "warn", "fail", "escalate"]


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"req-{make_uuid7()}")
    session_id: str | None = None
    user_goal: str
    repo_root: str
    task_type: TaskType
    risk_level: RiskLevel
    changed_files: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class ContextBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int
    premium_call_budget: int = 1
    cache_policy: CachePolicy = "prefer_cache"
    cheap_model: str = ""
    mid_model: str = ""
    premium_model: str = ""


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"rd-{make_uuid7()}")
    session_id: str
    request_id: str = ""
    step_index: int
    step_type: StepType
    risk_level: RiskLevel
    tier: ExecutionTier
    selected_model: str = ""
    confidence: float = Field(ge=0, le=1)
    reason: str
    protected_file_match: bool = False
    verifier_required: list[str] = Field(default_factory=list)
    escalation_trigger: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class VerificationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"ve-{make_uuid7()}")
    route_decision_id: str
    session_id: str
    changed_files: list[str] = Field(default_factory=list)
    validation_results: list[ValidationResult] = Field(default_factory=list)
    rubric_status: RubricStatus = "not_run"
    outcome: VerificationOutcome
    compressed_evidence: str = ""
    human_accepted: bool | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class RoutingEvalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    cost_per_accepted_patch: float
    premium_call_rate: float
    cheap_success_rate: float
    escalation_success_rate: float
    routing_regression_rate: float
    created_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "AgentRequest",
    "CachePolicy",
    "ContextBudgetPolicy",
    "ExecutionTier",
    "RiskLevel",
    "RouteDecision",
    "RoutingEvalSummary",
    "RubricStatus",
    "StepType",
    "TaskType",
    "VerificationEnvelope",
    "VerificationOutcome",
]
