from __future__ import annotations

import json
import subprocess

from lemoncrow.gateway.cli.commands import mcp


def _response(
    tools: list[str],
    *,
    server_name: str = "lc",
    instructions: str = "LemonCrow: http://127.0.0.1:7420 view view_test rev 0 (warm, 1 files, 0 hashed, 0 uploaded, 0.01s)",
    probe_error: bool = False,
) -> str:
    probe = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "content": [{"type": "text", "text": "no exact match"}],
            "isError": probe_error,
        },
    }
    return "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"serverInfo": {"name": server_name}, "instructions": instructions},
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": name} for name in tools]},
                }
            ),
            json.dumps(probe),
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
    assert result["server"] == "lc"


def test_probe_stdio_server_rejects_degraded_server(monkeypatch) -> None:
    monkeypatch.setattr(mcp.shutil, "which", lambda _name: "/venv/bin/lemoncrow")
    monkeypatch.setattr(
        mcp.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=_response(
                ["read", "edit", "code_search", "bash"],
                instructions="LemonCrow: server-side tools unavailable (server_unreachable: refused); client-side tools still work",
            ),
            stderr="",
        ),
    )

    result = mcp.probe_stdio_server()

    assert result["ok"] is False
    assert "server-side tools unavailable" in result["error"]


def test_probe_stdio_server_rejects_failed_code_search_probe(monkeypatch) -> None:
    monkeypatch.setattr(mcp.shutil, "which", lambda _name: "/venv/bin/lemoncrow")
    monkeypatch.setattr(
        mcp.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=_response(["read", "edit", "code_search", "bash"], probe_error=True), stderr=""
        ),
    )

    result = mcp.probe_stdio_server()

    assert result["ok"] is False
    assert "code_search probe failed" in result["error"]


def test_probe_stdio_server_falls_back_to_lc_entrypoint(monkeypatch) -> None:
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    monkeypatch.setattr(
        mcp.shutil,
        "which",
        lambda name: "/home/user/.local/bin/lc" if name == "lc" else None,
    )

    def run(command, **kwargs):
        commands.append(command)
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_response(["read", "edit", "code_search", "bash"]),
            stderr="",
        )

    monkeypatch.setattr(mcp.subprocess, "run", run)

    result = mcp.probe_stdio_server()

    assert result["ok"] is True
    assert commands == [["/home/user/.local/bin/lc", "mcp", "--host", "claude"]]
    assert environments[0]["LEMONCROW_STARTUP_BUDGET_S"] == "30.0"
    assert environments[0]["LEMONCROW_REQUEST_TIMEOUT_S"] == "30.0"
