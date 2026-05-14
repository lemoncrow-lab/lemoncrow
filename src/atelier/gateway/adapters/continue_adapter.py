"""Continue.dev middleware adapter for Atelier.

Continue.dev is a VS Code / JetBrains AI assistant that can call external
context providers.  This adapter exposes Atelier's reasoning blocks and
rubric gates as a Continue context-provider-compatible interface.

Usage (programmatic)::

    from atelier.gateway.adapters import ContinueAdapter, ContinueConfig
    from atelier.gateway.sdk import AtelierClient

    client = AtelierClient.local()
    adapter = ContinueAdapter.from_config(ContinueConfig(mode="suggest"), client=client)

    # Called from a Continue context provider:
    ctx = adapter.get_context(query="Add rate limiting to API")
    # ctx.context is injected into the Continue prompt

For VS Code / JetBrains integration see docs/integrations/continue.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from atelier.gateway.adapters.adapter_base import AdapterMode, AgentAdapter
from atelier.gateway.sdk import AtelierClient
from atelier.gateway.sdk.client import ContextResult


class ContinueConfig(BaseModel):
    """Configuration for the Continue.dev adapter."""

    model_config = ConfigDict(extra="forbid")

    mode: AdapterMode = "shadow"
    default_domain: str | None = None
    default_rubric_id: str | None = None
    default_tools: list[str] = []
    server_url: str = "http://localhost:8123"


@dataclass
class ContinueAdapter(AgentAdapter):
    """Atelier adapter for Continue.dev.

    Exposes Atelier reasoning context through Continue's context-provider
    interface so relevant reasoning blocks are injected into every prompt.

    Provides:
    - ``get_context``    - retrieve relevant reasoning blocks for a query
    - ``server_url``     - URL of the Atelier HTTP service (for remote mode)
    """

    host: str = "continue.dev"
    default_rubric_id: str | None = None
    server_url: str = "http://localhost:8123"

    @classmethod
    def from_config(cls, config: ContinueConfig, *, client: AtelierClient) -> ContinueAdapter:
        """Create an adapter from a ``ContinueConfig``."""
        return cls(
            client=client,
            mode=config.mode,
            host="continue.dev",
            default_domain=config.default_domain,
            default_tools=list(config.default_tools),
            default_rubric_id=config.default_rubric_id,
            server_url=config.server_url,
        )

    def get_context(
        self,
        *,
        query: str,
        domain: str | None = None,
        files: list[str] | None = None,
    ) -> ContextResult:
        """Return relevant reasoning context for a Continue query string.

        This is the main entry point for a Continue context provider.
        The returned ``context`` string can be prepended to the chat prompt.
        """
        return self.get_context(task=query, domain=domain, files=files)

    @classmethod
    def install(cls) -> str:
        """Return installation instructions for Continue.dev integration."""
        return (
            "# Continue.dev ← Atelier integration\n"
            "1. pip install atelier-runtime\n"
            "2. atelier init && atelier serve   # starts HTTP service on :8123\n"
            "3. Add to ~/.continue/config.json:\n"
            '   { "name": "Atelier",\n'
            '     "contextProviders": [{\n'
            '       "name": "http",\n'
            '       "params": {\n'
            '         "url": "http://localhost:8123/context",\n'
            '         "title": "Atelier reasoning blocks"\n'
            "     }]\n"
            " }\n"
            "See docs/integrations/continue.md for full reference."
        )
