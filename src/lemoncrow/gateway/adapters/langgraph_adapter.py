"""LangGraph middleware adapter for LemonCrow.

Wrap the stable LemonCrowClient SDK so LangGraph graphs can call LemonCrow
programmatically at node boundaries:
- ``node_pre_check``       - reasoning-block gate before a node runs
- ``edge_rubric_gate``     - rubric gate on a conditional edge
- ``node_failure_recovery``- failure analysis when a node raises

Usage::

    from langgraph.graph import StateGraph
    from lemoncrow.gateway.adapters import LangGraphAdapter, LangGraphConfig
    from lemoncrow.gateway.sdk import LemonCrowClient

    client = LemonCrowClient.local()
    lemoncrow = LangGraphAdapter.from_config(
        LangGraphConfig(
            mode="suggest",
            node_domain_map={"plan_node": "Agent.codegen"},
        ),
        client=client,
    )

    def plan_node(state):
        decision = lemoncrow.node_context("plan_node", task=state["task"])
        if decision.blocked:
            return {"error": decision.warnings}
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from lemoncrow.gateway.adapters.adapter_base import AdapterDecision, AdapterMode, AgentAdapter
from lemoncrow.gateway.sdk import LemonCrowClient
from lemoncrow.gateway.sdk.client import SavingsSummary


class LangGraphConfig(BaseModel):
    """Configuration for the LangGraph adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_tools: list[str] = []
    node_domain_map: dict[str, str] = {}


@dataclass
class LangGraphAdapter(AgentAdapter):
    """LemonCrow adapter for LangGraph.

    Allows LangGraph nodes and edges to call LemonCrow at runtime:
    - ``node_pre_check``        - pre-plan gate at a node boundary
    - ``edge_rubric_gate``      - rubric check on a conditional edge
    - ``node_failure_recovery`` - failure analysis when a node raises
    - ``graph_savings``         - cost-savings summary for a completed graph run
    """

    host: str = "langgraph"
    node_domain_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: LangGraphConfig, *, client: LemonCrowClient) -> LangGraphAdapter:
        """Create an adapter from a ``LangGraphConfig``."""
        return cls(
            client=client,
            mode=config.mode,
            host="langgraph",
            default_domain=config.default_domain,
            default_tools=list(config.default_tools),
            node_domain_map=dict(config.node_domain_map),
        )

    def _domain_for(self, node_name: str) -> str | None:
        """Return the domain for a node, falling back to ``default_domain``."""
        return self.node_domain_map.get(node_name) or self.default_domain

    def node_context(
        self,
        node_name: str,
        *,
        task: str,
        files: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> AdapterDecision:
        """Fetch reasoning context at a LangGraph node boundary.

        The domain is resolved from ``node_domain_map[node_name]`` if present,
        falling back to ``default_domain``.
        """
        domain = self._domain_for(node_name)
        context = self.get_context(
            task=task,
            domain=domain,
            files=files,
            tools=tools,
        )
        return AdapterDecision(
            host=self.host,
            mode=self.mode,
            blocked=False,
            reasoning_context=context.context,
        )

    def edge_rubric_gate(
        self,
        node_name: str,
        *,
        rubric_id: str,
        checks: dict[str, bool | None],
    ) -> AdapterDecision:
        """Run a rubric gate on a LangGraph conditional edge."""
        return self.verify_rubric(rubric_id=rubric_id, checks=checks)

    def node_failure_recovery(
        self,
        node_name: str,
        *,
        task: str,
        error: str,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> AdapterDecision:
        """Analyse a node failure and return a recovery hint."""
        return self.analyze_failure(
            task=task,
            error=error,
            domain=self._domain_for(node_name),
            files=files,
            recent_actions=recent_actions,
        )

    def graph_savings(self) -> SavingsSummary:
        """Return cost-savings summary for the current graph run."""
        return self.benchmark_report()

    @classmethod
    def install(cls) -> str:
        """Return installation instructions for LangGraph integration."""
        return (
            "# LangGraph ← LemonCrow integration\n"
            "1. pip install lemoncrow\n"
            "2. lemon init\n"
            "3. Instantiate LangGraphAdapter in your graph builder:\n"
            "    lemoncrow = LangGraphAdapter(client=LemonCrowClient.local(), mode='suggest')\n"
            "    # In each node function:\n"
            "    decision = lemoncrow.node_context(node_name, task=task)\n"
            "    if decision.blocked: raise ValueError(decision.warnings)\n"
            "\n"
            "See docs/integrations/langgraph.md for full reference."
        )
