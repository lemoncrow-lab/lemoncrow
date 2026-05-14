"""Shared middleware contract for external agent ecosystems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from atelier.core.foundation.models import RescueResult, RubricResult
from atelier.gateway.sdk import (
    AtelierClient,
    ContextResult,
    FailureAnalysisResult,
    SavingsSummary,
)

AdapterMode = Literal["shadow", "suggest", "enforce"]


class AdapterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    mode: AdapterMode
    blocked: bool
    reasoning_context: str = ""
    warnings: list[str] = Field(default_factory=list)
    rubric_result: RubricResult | None = None
    rescue_result: RescueResult | None = None


@dataclass
class AgentAdapter:
    client: AtelierClient
    mode: AdapterMode = "shadow"
    host: str = "generic"
    default_domain: str | None = None
    default_tools: list[str] = field(default_factory=list)

    def get_context(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> ContextResult:
        return self.client.get_context(
            task=task,
            domain=domain or self.default_domain,
            files=files,
            tools=tools or self.default_tools,
        )

    def verify_rubric(
        self,
        *,
        rubric_id: str,
        checks: dict[str, bool | None],
    ) -> AdapterDecision:
        rubric_result = self.client.run_rubric_gate(rubric_id=rubric_id, checks=checks)
        blocked = self.mode == "enforce" and rubric_result.status == "blocked"
        warnings = [outcome.name for outcome in rubric_result.outcomes if outcome.status in {"warn", "fail", "missing"}]
        return AdapterDecision(
            host=self.host,
            mode=self.mode,
            blocked=blocked,
            warnings=warnings,
            rubric_result=rubric_result,
        )

    def analyze_failure(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> AdapterDecision:
        rescue = self.client.rescue_failure(
            task=task,
            error=error,
            domain=domain or self.default_domain,
            files=files,
            recent_actions=recent_actions,
        )
        blocked = self.mode == "enforce"
        return AdapterDecision(
            host=self.host,
            mode=self.mode,
            blocked=blocked,
            rescue_result=rescue,
            warnings=[rescue.rescue],
        )

    def benchmark_report(self) -> SavingsSummary:
        return self.client.savings.summary()

    def failure_clusters(self, *, domain: str | None = None, limit: int = 100) -> FailureAnalysisResult:
        return self.client.failures.analyze(domain=domain or self.default_domain, limit=limit)
