"""OpenHands middleware adapter for Atelier.

Wrap the stable AtelierClient SDK so OpenHands agents can:
- Run a pre-plan reasoning-block check before executing a task
- Apply a rubric gate to validate outputs
- Rescue from failures with Atelier's analysis engine
- Report benchmark savings

Usage::

    from atelier.gateway.adapters import OpenHandsAdapter, OpenHandsConfig
    from atelier.gateway.sdk import AtelierClient

    client = AtelierClient.local()
    adapter = OpenHandsAdapter.from_config(OpenHandsConfig(mode="suggest"), client=client)
    decision = adapter.get_context_and_gate(
        task="Refactor auth module",
    )
    if decision.blocked:
        raise RuntimeError(decision.warnings)
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from atelier.gateway.adapters.adapter_base import AdapterDecision, AdapterMode, AgentAdapter
from atelier.gateway.sdk import AtelierClient
from atelier.gateway.sdk.client import SavingsSummary


class OpenHandsConfig(BaseModel):
    """Configuration for the OpenHands adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_rubric_id: str | None = None
    default_tools: list[str] = []
    auto_rescue: bool = True


@dataclass
class OpenHandsAdapter(AgentAdapter):
    """Atelier adapter for the OpenHands agent framework.

    Provides:
    - ``get_context_and_gate`` - pre-task check + optional rubric gate
    - ``rescue``               - failure analysis with recovery hint
    - ``savings``              - cost-tracker benchmark summary
    """

    host: str = "openhands"
    default_rubric_id: str | None = None
    auto_rescue: bool = True

    @classmethod
    def from_config(cls, config: OpenHandsConfig, *, client: AtelierClient) -> OpenHandsAdapter:
        """Create an adapter from an ``OpenHandsConfig``."""
        return cls(
            client=client,
            mode=config.mode,
            host="openhands",
            default_domain=config.default_domain,
            default_tools=list(config.default_tools),
            default_rubric_id=config.default_rubric_id,
            auto_rescue=config.auto_rescue,
        )

    def get_context_and_gate(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
        rubric_id: str | None = None,
        checks: dict[str, bool | None] | None = None,
    ) -> AdapterDecision:
        """Fetch reasoning context + optional rubric gate.

        shadow  - never blocks; logs warnings only.
        suggest - surfaces warnings; execution continues.
        enforce - callers must check ``decision.blocked``.
        """
        context = self.get_context(task=task, domain=domain, files=files, tools=tools)
        decision = AdapterDecision(
            host=self.host,
            mode=self.mode,
            blocked=False,
            reasoning_context=context.context,
        )
        rid = rubric_id or self.default_rubric_id
        if rid and checks:
            rubric_decision = self.verify_rubric(rubric_id=rid, checks=checks)
            decision.warnings.extend(rubric_decision.warnings)
            decision.rubric_result = rubric_decision.rubric_result
            if rubric_decision.blocked:
                decision.blocked = True
        return decision

    def rescue(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> AdapterDecision:
        """Analyse a failure and return a recovery hint."""
        return self.analyze_failure(
            task=task,
            error=error,
            domain=domain,
            files=files,
            recent_actions=recent_actions,
        )

    def savings(self) -> SavingsSummary:
        """Return cost-savings summary from the embedded tracker."""
        return self.benchmark_report()

    @classmethod
    def install(cls) -> str:
        """Return installation instructions for OpenHands integration."""
        return (
            "# OpenHands ← Atelier integration\n"
            "1. pip install atelier-runtime\n"
            "2. atelier init              # creates ~/.atelier/\n"
            "3. In your OpenHands agent hook:\n"
            "    adapter = OpenHandsAdapter(client=AtelierClient.local(), mode='suggest')\n"
            "    decision = adapter.get_context_and_gate(task=task)\n"
            "    if decision.blocked: return decision.warnings\n"
            "\n"
            "See docs/integrations/openhands.md for full reference."
        )
