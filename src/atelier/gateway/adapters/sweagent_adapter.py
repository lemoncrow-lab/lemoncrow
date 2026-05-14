"""SWE-agent middleware adapter for Atelier.

Wrap the stable AtelierClient SDK so SWE-agent can:
- Prime a reasoning context before tackling a GitHub issue
- Rescue from tool failures with Atelier's failure-analysis engine
- Record traces for post-run inspection and pattern mining

Usage::

    from atelier.gateway.adapters import SWEAgentAdapter, SWEAgentConfig
    from atelier.gateway.sdk import AtelierClient

    client = AtelierClient.local()
    adapter = SWEAgentAdapter.from_config(SWEAgentConfig(mode="suggest"), client=client)

    # before starting a repo task
    ctx = adapter.prime_context(task="Fix failing test in auth module")

    # after a tool call fails
    recovery = adapter.rescue_on_failure(
        task="Fix failing test", error="AssertionError: expected 200 got 403"
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from atelier.gateway.adapters.adapter_base import AdapterDecision, AdapterMode, AgentAdapter
from atelier.gateway.sdk import AtelierClient
from atelier.gateway.sdk.client import ContextResult


class SWEAgentConfig(BaseModel):
    """Configuration for the SWE-agent adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_tools: list[str] = []
    rescue_on_error: bool = True
    max_rescue_attempts: int = 3


@dataclass
class SWEAgentAdapter(AgentAdapter):
    """Atelier adapter for SWE-agent.

    Provides:
    - ``prime_context``      - retrieve reasoning blocks relevant to the task
    - ``rescue_on_failure``  - analyse a tool failure and return a recovery hint
    - ``record_run``         - store a trace after the task completes
    """

    host: str = "swe-agent"
    rescue_on_error: bool = True
    max_rescue_attempts: int = 3

    @classmethod
    def from_config(cls, config: SWEAgentConfig, *, client: AtelierClient) -> SWEAgentAdapter:
        """Create an adapter from a ``SWEAgentConfig``."""
        return cls(
            client=client,
            mode=config.mode,
            host="swe-agent",
            default_domain=config.default_domain,
            default_tools=list(config.default_tools),
            rescue_on_error=config.rescue_on_error,
            max_rescue_attempts=config.max_rescue_attempts,
        )

    def prime_context(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> ContextResult:
        """Retrieve relevant reasoning blocks for a task before execution begins."""
        return self.get_context(task=task, domain=domain, files=files, tools=tools)

    def rescue_on_failure(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> AdapterDecision:
        """Analyse a tool failure and return a structured recovery hint."""
        return self.analyze_failure(
            task=task,
            error=error,
            domain=domain,
            files=files,
            recent_actions=recent_actions,
        )

    def record_run(
        self,
        *,
        task: str,
        status: str,
        domain: str | None = None,
        files_touched: list[str] | None = None,
        commands_run: list[str] | None = None,
        errors_seen: list[str] | None = None,
        diff_summary: str = "",
    ) -> None:
        """Record a trace for this SWE-agent run."""
        from atelier.core.foundation.models import TraceStatus

        safe_status: TraceStatus = (
            status if status in {"success", "failure", "partial", "skipped"} else "failure"  # type: ignore[assignment]
        )
        self.client.traces.record(
            agent=self.host,
            domain=domain or self.default_domain or "Agent.sweagent",
            task=task,
            status=safe_status,
            files_touched=cast(Any, files_touched),
            commands_run=cast(Any, commands_run),
            errors_seen=errors_seen,
            diff_summary=diff_summary,
        )

    @classmethod
    def install(cls) -> str:
        """Return installation instructions for SWE-agent integration."""
        return (
            "# SWE-agent ← Atelier integration\n"
            "1. pip install atelier-runtime\n"
            "2. atelier init\n"
            "3. In your SWE-agent run_agent hook:\n"
            "   adapter = SWEAgentAdapter(client=AtelierClient.local(), mode='suggest')\n"
            "   ctx = adapter.prime_context(task=issue_title)\n"
            "   # inject ctx.context into the system prompt\n"
            "See docs/integrations/sweagent.md for full reference."
        )
