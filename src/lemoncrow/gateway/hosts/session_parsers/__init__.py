"""Host-specific session parsers and adapters.

This module provides integration adapters for each supported agent CLI:
- Claude (session parsing, artifact handling)
- Codex (session parsing, run import)
- Copilot (session parsing, interaction handling)
- OpenCode (session parsing, workspace context)

All adapters inherit from AgentSessionAdapter base class.
"""

from lemoncrow.gateway.hosts.session_parsers.claude import ClaudeImporter
from lemoncrow.gateway.hosts.session_parsers.codex import CodexImporter
from lemoncrow.gateway.hosts.session_parsers.copilot import CopilotImporter
from lemoncrow.gateway.hosts.session_parsers.opencode import OpenCodeImporter
from lemoncrow.gateway.hosts.session_parsers.registry import SUPPORTED_SESSION_IMPORT_HOSTS

__all__ = [
    "SUPPORTED_SESSION_IMPORT_HOSTS",
    "ClaudeImporter",
    "CodexImporter",
    "CopilotImporter",
    "OpenCodeImporter",
]
