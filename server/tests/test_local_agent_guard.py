"""The loopback server refuses human review judgment from a coding-agent process.

The machine token is readable by any process of the same user, so the server
looks at the process that actually opened the connection and its ancestry.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_agent_guard import (
    ProcessRecord,
    agent_host_in_ancestry,
    caller_agent_host,
    is_judgment_request,
)


@pytest.mark.parametrize(
    ("method", "path", "judgment"),
    [
        ("POST", "/api/reviews/rv-1/marks", True),
        ("POST", "/api/reviews/rv-1/marks/bulk", True),
        ("POST", "/api/reviews/rv-1/finish", True),
        ("POST", "/api/reviews/rv-1/outcome", True),
        ("POST", "/api/reviews/rv-1/annotations", True),
        ("PATCH", "/api/annotations/ann-1", True),
        ("POST", "/api/reviews/rv-1/verification/waivers", True),
        ("POST", "/api/reviews/rv-1/landing/arm", True),
        ("POST", "/api/reviews/rv-1/github/publish", True),
        # Agents legitimately report checks, refresh, ask and read.
        ("POST", "/api/reviews/rv-1/verification", False),
        ("POST", "/api/reviews/rv-1/refresh", False),
        ("POST", "/api/reviews/rv-1/ask", False),
        ("POST", "/api/reviews/rv-1/landing/cancel", False),
        ("GET", "/api/reviews/rv-1/marks", False),
        ("GET", "/api/reviews/rv-1/annotations", False),
    ],
)
def test_judgment_routes_are_the_human_only_writes(method: str, path: str, judgment: bool) -> None:
    assert is_judgment_request(method, path) is judgment


def test_agent_evidence_anywhere_in_the_ancestry_names_the_host() -> None:
    table = {
        40: ProcessRecord(ppid=30, names=("curl",), markers=()),
        30: ProcessRecord(ppid=20, names=("sh",), markers=("CLAUDECODE",)),
        20: ProcessRecord(ppid=10, names=("node", "claude"), markers=()),
        10: ProcessRecord(ppid=1, names=("zsh",), markers=()),
    }
    assert agent_host_in_ancestry(40, table.get) == "claude"
    # A child that cleared its own environment is still under the agent binary.
    table[30] = ProcessRecord(ppid=20, names=("env",), markers=())
    assert agent_host_in_ancestry(40, table.get) == "claude"
    human = {
        40: ProcessRecord(ppid=10, names=("lc",), markers=()),
        10: ProcessRecord(ppid=1, names=("zsh",), markers=()),
    }
    assert agent_host_in_ancestry(40, human.get) == ""
    # A cycle or a vanished parent ends the walk without guessing.
    assert agent_host_in_ancestry(7, {7: ProcessRecord(ppid=7, names=("x",), markers=())}.get) == ""


def test_the_server_and_the_process_that_launched_it_are_its_operator() -> None:
    # claude(20) -> sh(30, agent env) -> pytest(40) -> server(50); curl(60) is
    # another child of pytest.
    table = {
        60: ProcessRecord(ppid=40, names=("curl",), markers=()),
        50: ProcessRecord(ppid=40, names=("python",), markers=()),
        40: ProcessRecord(ppid=30, names=("pytest",), markers=()),
        30: ProcessRecord(ppid=20, names=("sh",), markers=("CLAUDECODE",)),
        20: ProcessRecord(ppid=1, names=("claude",), markers=()),
    }
    assert caller_agent_host(50, own=50, lookup=table.get) == ""
    assert caller_agent_host(40, own=50, lookup=table.get) == ""
    assert caller_agent_host(60, own=50, lookup=table.get) == "claude"


_POST = """
import sys, urllib.request, urllib.error
request = urllib.request.Request(
    sys.argv[1], data=b"{}", method="POST",
    headers={"Authorization": "Bearer " + sys.argv[2], "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.status); print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.code); print(exc.read().decode())
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="peer-process lookup is exercised on Linux")
def test_machine_token_judgment_from_an_agent_process_is_refused(tmp_path: Path) -> None:
    asyncio.run(_exercise_guard(tmp_path))


async def _exercise_guard(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", token_file=token_file, allow_local_fs=False)
    )
    token = token_file.read_text(encoding="utf-8").strip()
    url = await server.start()
    try:
        marks = f"{url}/api/reviews/rv-missing/marks"
        env = {**os.environ, "CLAUDECODE": "1"}
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", _POST, marks, token],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        status, body = result.stdout.split("\n", 1)
        assert status == "403", result.stdout
        assert "coding-agent process (claude)" in json.loads(body)["error"]["message"]

        # The server's own process is not an agent: in-process callers reach
        # the Review API and get its ordinary answer.
        async with ClientSession() as client:
            own = await client.post(marks, json={}, headers={"Authorization": f"Bearer {token}"})
            assert own.status != 403, await own.text()

            # Non-judgment writes stay open to agents.
            check = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", _POST, f"{url}/api/reviews/rv-missing/verification", token],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            assert check.stdout.split("\n", 1)[0] != "403", check.stdout
    finally:
        await server.stop()
