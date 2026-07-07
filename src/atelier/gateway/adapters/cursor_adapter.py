"""Cursor IDE middleware adapter for Atelier.

Cursor is an AI-native IDE built on VS Code that supports MCP servers via
``~/.cursor/mcp.json`` (global) or ``.cursor/mcp.json`` (project-local).

This adapter wraps Atelier's reasoning context and rubric gates so Cursor's
agent (the Cursor Tab / Composer) can:

- Retrieve relevant Playbooks before tackling a task
- Run rubric gates to validate results
- Rescue from failures with recovery hints
- Record traces for post-run analysis

Installation (automatic via the installer)::

    atelier install cursor

Or manually — add to ``~/.cursor/mcp.json``::

    {
      "mcpServers": {
        "atelier": {
          "type": "stdio",
          "command": "atelier",
          "args": ["mcp"]
        }
      }
    }

Then add a global rule at ``~/.cursor/rules/atelier.mdc`` (Cursor Pro)
or a project-local rule at ``.cursor/rules/atelier.mdc`` so the agent
knows to prefer ``atelier`` tools over native grep/read.

Usage (programmatic)::

    from atelier.gateway.adapters import CursorAdapter, CursorConfig
    from atelier.gateway.sdk import AtelierClient

    client = AtelierClient.local()
    adapter = CursorAdapter.from_config(CursorConfig(mode="suggest"), client=client)
    ctx = adapter.prime_context(task="Add rate limiting to API")
    # ctx.context is injected into the system prompt

See docs/integrations/cursor.md for full reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from atelier.gateway.adapters.adapter_base import AdapterDecision, AdapterMode, AgentAdapter
from atelier.gateway.sdk import AtelierClient
from atelier.gateway.sdk.client import ContextResult


class CursorConfig(BaseModel):
    """Configuration for the Cursor IDE adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_tools: list[str] = []
    agent_label: str = "cursor"


@dataclass
class CursorAdapter(AgentAdapter):
    """Atelier adapter for Cursor IDE.

    Cursor exposes Atelier through its MCP server mechanism, meaning
    discovery and invocation happen via ``~/.cursor/mcp.json``.

    Provides:
    - ``prime_context``    - retrieve reasoning blocks relevant to a task
    - ``get_context``      - low-level context retrieval
    - ``get_decision``     - context + decision with warnings
    """

    host: str = "cursor"

    @classmethod
    def from_config(cls, config: CursorConfig, *, client: AtelierClient) -> CursorAdapter:
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
            "# Cursor IDE ← Atelier integration\n"
            "1. pip install atelier-ws\n"
            "2. atelier init && atelier mcp    # verify MCP server starts\n"
            "3. Add to ~/.cursor/mcp.json:\n"
            '   { "mcpServers": {\n'
            '       "atelier": {\n'
            '           "type": "stdio",\n'
            '           "command": "atelier",\n'
            '           "args": []\n'
            "       }\n"
            "   }}\n"
            "4. (Optional) Create ~/.cursor/rules/atelier.mdc with:\n"
            "   ---\n"
            "   description: Atelier reasoning context usage guide\n"
            "   alwaysApply: true\n"
            "   ---\n"
            "   \n"
            "   Use atelier's `context` tool at the start of every task "
            "and `trace`/`rescue` after completions.\n"
            "See docs/integrations/cursor.md for full reference."
        )
