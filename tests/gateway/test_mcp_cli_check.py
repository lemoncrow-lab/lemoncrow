from __future__ import annotations

import json
import subprocess

from lemoncrow.gateway.cli.commands import mcp


def _response(tools: list[str]) -> str:
    return "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "lemoncrow"}}}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": name} for name in tools]},
                }
            ),
        ]
    )


def test_probe_stdio_server_requires_core_tools(monkeypatch) -> None:
    monkeypatch.setattr(mcp.shutil, "which", lambda _name: "/venv/bin/lemoncrow")
    monkeypatch.setattr(
        mcp.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=_response(["read", "edit", "code_search"]), stderr=""
        ),
    )

    result = mcp.probe_stdio_server()

    assert result["ok"] is False
    assert result["error"] == "missing tools: bash"


def test_probe_stdio_server_accepts_initialized_core_surface(monkeypatch) -> None:
    monkeypatch.setattr(mcp.shutil, "which", lambda _name: "/venv/bin/lemoncrow")
    monkeypatch.setattr(
        mcp.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=_response(["read", "edit", "code_search", "bash", "web_fetch"]), stderr=""
        ),
    )

    result = mcp.probe_stdio_server()

    assert result["ok"] is True
    assert result["server"] == "lemoncrow"
