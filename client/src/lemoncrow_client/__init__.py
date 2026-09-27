"""The public LemonCrow thin client.

One short-lived MCP stdio process. No daemon, no listening port, no unit, no
self-update, no downloaded binary, no product telemetry, and no egress other
than the configured LemonCrow endpoint.

Python standard library only -- ``pyproject.toml`` declares no dependencies and
``tests/test_packaging_audit.py`` asserts it, along with every other claim in
that sentence, against the installed package rather than against this docstring.
"""

from __future__ import annotations

from typing import Final

__all__ = ["CLIENT_NAME", "CLIENT_VERSION"]

CLIENT_NAME: Final[str] = "lemoncrow-client"
CLIENT_VERSION: Final[str] = "0.1.0"
