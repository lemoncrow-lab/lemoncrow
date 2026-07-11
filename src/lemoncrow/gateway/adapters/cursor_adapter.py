"""Cursor IDE middleware adapter for LemonCrow.

Cursor is an AI-native IDE built on VS Code that supports MCP servers via
``~/.cursor/mcp.json`` (global) or ``.cursor/mcp.json`` (project-local).

This adapter wraps LemonCrow's reasoning context and rubric gates so Cursor's
agent (the Cursor Tab / Composer) can:

- Retrieve relevant Playbooks before tackling a task
- Run rubric gates to validate results
- Rescue from failures with recovery hints
- Record traces for post-run analysis

Installation (automatic via the installer)::

    lemon install cursor

Or manually — add to ``~/.cursor/mcp.json``::

    {
      "mcpServers": {
        "lemoncrow": {
          "type": "stdio",
          "command": "lemon",
          "args": ["mcp"]
        }
      }
    }

Then add a global rule at ``~/.cursor/rules/lemoncrow.mdc`` (Cursor Pro)
or a project-local rule at ``.cursor/rules/lemoncrow.mdc`` so the agent
knows to prefer ``lemon`` tools over native grep/read.

Usage (programmatic)::

    from lemoncrow.gateway.adapters import CursorAdapter, CursorConfig
    from lemoncrow.gateway.sdk import LemonCrowClient

    client = LemonCrowClient.local()
    adapter = CursorAdapter.from_config(CursorConfig(mode="suggest"), client=client)
    ctx = adapter.prime_context(task="Add rate limiting to API")
    # ctx.context is injected into the system prompt

See docs/integrations/cursor.md for full reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from lemoncrow.gateway.adapters.adapter_base import AdapterDecision, AdapterMode, AgentAdapter
from lemoncrow.gateway.sdk import LemonCrowClient
from lemoncrow.gateway.sdk.client import ContextResult


class CursorConfig(BaseModel):
    """Configuration for the Cursor IDE adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_tools: list[str] = []
    agent_label: str = "cursor"


@dataclass
class CursorAdapter(AgentAdapter):
    """LemonCrow adapter for Cursor IDE.

    Cursor exposes LemonCrow through its MCP server mechanism, meaning
    discovery and invocation happen via ``~/.cursor/mcp.json``.

    Provides:
    - ``prime_context``    - retrieve reasoning blocks relevant to a task
    - ``get_context``      - low-level context retrieval
    - ``get_decision``     - context + decision with warnings
    """

    host: str = "cursor"

    @classmethod
    def from_config(cls, config: CursorConfig, *, client: LemonCrowClient) -> CursorAdapter:
        """Create an adapter from a ``CursorConfig``."""
        return cls(
            client=client,
            mode=config.mode,
            host="cursor",
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

        This is the main entry point called from a Cursor agent wrapper.
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
        """Return installation instructions for Cursor IDE integration."""
        return (
            "# Cursor IDE ← LemonCrow integration\n"
            "1. pip install lemoncrow\n"
            "2. lemon init && lemon mcp    # verify MCP server starts\n"
            "3. Add to ~/.cursor/mcp.json:\n"
            '   { "mcpServers": {\n'
            '       "lemoncrow": {\n'
            '           "type": "stdio",\n'
            '           "command": "lemon",\n'
            '           "args": []\n'
            "       }\n"
            "   }}\n"
            "4. (Optional) Create ~/.cursor/rules/lemoncrow.mdc with:\n"
            "   ---\n"
            "   description: LemonCrow reasoning context usage guide\n"
            "   alwaysApply: true\n"
            "   ---\n"
            "   \n"
            "   Use LemonCrow's `context` tool at the start of every task "
            "and `trace`/`rescue` after completions.\n"
            "See docs/integrations/cursor.md for full reference."
        )
