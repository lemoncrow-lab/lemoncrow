"""Hermes Agent middleware adapter for LemonCrow.

Hermes Agent (by Nous Research) is a general-purpose agent framework that
supports MCP servers through its ``config.yaml`` (at ``$HERMES_HOME/config.yaml``
or ``~/.hermes/config.yaml``).

This adapter wraps LemonCrow's reasoning context and rubric gates so Hermes
agents can:

- Retrieve relevant Playbooks before tackling a task
- Run rubric gates to validate results
- Rescue from failures with recovery hints
- Record traces for post-run analysis and pattern mining

Hermes config format (YAML)::

    mcp_servers:
      lc:
        command: lemoncrow mcp
        args: []
        timeout: 120
        connect_timeout: 60
        enabled: true

    platform_toolsets:
      cli:
        - hermes-cli
        - mcp-lemoncrow

Usage (programmatic)::

    from lemoncrow.gateway.adapters import HermesAdapter, HermesConfig
    from lemoncrow.gateway.sdk import LemonCrowClient

    client = LemonCrowClient.local()
    adapter = HermesAdapter.from_config(HermesConfig(mode="suggest"), client=client)
    ctx = adapter.prime_context(task="Refactor auth module")
    # ctx.context is injected into the Hermes agent's system prompt

See docs/integrations/hermes.md for full reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from lemoncrow.gateway.adapters.adapter_base import AdapterDecision, AdapterMode, AgentAdapter
from lemoncrow.gateway.sdk import LemonCrowClient
from lemoncrow.gateway.sdk.client import ContextResult


class HermesConfig(BaseModel):
    """Configuration for the Hermes Agent adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_tools: list[str] = []


@dataclass
class HermesAdapter(AgentAdapter):
    """LemonCrow adapter for Hermes Agent (Nous Research).

    Provides:
    - ``prime_context``    - retrieve reasoning blocks relevant to a task
    - ``get_context``      - low-level context retrieval
    - ``get_decision``     - context + decision with warnings
    - ``record_run``       - store a trace after the task completes
    """

    host: str = "hermes"

    @classmethod
    def from_config(cls, config: HermesConfig, *, client: LemonCrowClient) -> HermesAdapter:
        """Create an adapter from a ``HermesConfig``."""
        return cls(
            client=client,
            mode=config.mode,
            host="hermes",
            default_domain=config.default_domain,
            default_tools=list(config.default_tools),
        )

    def prime_context(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> ContextResult:
        """Retrieve relevant reasoning blocks for a task before execution begins.

        This is the main entry point called from a Hermes agent wrapper.
        The returned ``context`` string can be prepended to the system prompt.
        """
        return self.get_context(task=task, domain=domain, files=files, tools=tools)

    def get_decision(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> AdapterDecision:
        """Fetch reasoning context and return an AdapterDecision.

        ``shadow``  - never blocks; logs warnings only.
        ``suggest`` - surfaces warnings; execution continues.
        ``enforce`` - callers must check ``decision.blocked``.
        """
        context = self.get_context(task=task, domain=domain, files=files, tools=tools)
        return AdapterDecision(
            host=self.host,
            mode=self.mode,
            blocked=False,
            reasoning_context=context.context,
        )

    @classmethod
    def install(cls) -> str:
        """Return installation instructions for Hermes Agent integration."""
        return (
            "# Hermes Agent ← LemonCrow integration\n"
            "1. pip install lemoncrow\n"
            "2. lemoncrow init && lemoncrow mcp    # verify MCP server starts\n"
            "3. Add to $HERMES_HOME/config.yaml or ~/.hermes/config.yaml:\n"
            "   mcp_servers:\n"
            "     lc:\n"
            "       command: lemoncrow mcp\n"
            "       args: []\n"
            "       timeout: 120\n"
            "       connect_timeout: 60\n"
            "       enabled: true\n"
            "   \n"
            "   platform_toolsets:\n"
            "     cli:\n"
            "       - hermes-cli\n"
            "       - mcp-lemoncrow\n"
            "See docs/integrations/hermes.md for full reference."
        )
