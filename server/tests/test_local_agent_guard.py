"""The loopback server refuses human review judgment from a coding-agent process.

The machine token is readable by any process of the same user, so the server
looks at the process that actually opened the connection and its ancestry.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core import local_app
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_agent_guard import (
    UNIDENTIFIED_CALLER,
    ProcessRecord,
    agent_host_in_ancestry,
    caller_agent_host,
    is_attributed_agent_decision,
    is_judgment_request,
    peer_agent_host,
)

DECISION = {
    "finding_disposition": "fixed",
    "finding_disposition_reason": "",
    "finding_disposition_evidence": ["ev-1"],
    "expected_revision_id": "rv-9",
    "actor": "agent",
    "agent_host": "claude",
}


def test_only_an_agents_own_attributed_finding_decision_passes() -> None:
    def passes(body: object, *, host: str = "claude", method: str = "PATCH", path: str = "/api/annotations/a") -> bool:
        return is_attributed_agent_decision(method, path, json.dumps(body).encode(), host)

    assert passes(DECISION)
    assert passes({**DECISION, "finding_disposition": "dismissed", "finding_disposition_reason": "incorrect"})
    assert passes({**DECISION, "finding_disposition": "open"})
    # Confirming is the reviewer's call; a decision not named as the agent's, or named as another agent's,
    # claims the reviewer's judgment.
    assert not passes({**DECISION, "finding_disposition": "confirmed_by_reviewer"})
    assert not passes({key: value for key, value in DECISION.items() if key != "actor"})
    assert not passes({**DECISION, "actor": "human"})
    assert not passes(DECISION, host="codex")
    assert not passes(DECISION, host="")
    assert not passes(DECISION, host=UNIDENTIFIED_CALLER)
    # Only the decision: no body edit, no comment state, on one annotation, by PATCH.
    assert not passes({**DECISION, "body": "rewritten"})
    assert not passes({**DECISION, "state": "resolved"})
    assert not passes(DECISION, method="POST", path="/api/reviews/rv-1/annotations")
    assert not passes(DECISION, path="/api/annotations/a/extra")
    assert not passes([DECISION])
    assert not is_attributed_agent_decision("PATCH", "/api/annotations/a", b"not json", "claude")


def test_an_agents_attributed_decision_reaches_the_review_api_with_its_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_exercise_decision(tmp_path, monkeypatch))


async def _exercise_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", token_file=token_file, allow_local_fs=False)
    )
    token = token_file.read_text(encoding="utf-8").strip()
    url = await server.start()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with ClientSession() as client:

            async def patch(body: dict[str, object], host: str) -> tuple[int, str]:
                monkeypatch.setattr(local_app, "peer_agent_host", lambda _transport: host)
                answer = await client.patch(f"{url}/api/annotations/ann-missing", json=body, headers=headers)
                return answer.status, await answer.text()

            person = await patch(DECISION, "")
            agent = await patch(DECISION, "claude")
            # Past the guard, the Review API answers the agent exactly as it answers a person.
            assert agent[0] != 403 and agent == person, (agent, person)
            for refused in (
                {**DECISION, "finding_disposition": "confirmed_by_reviewer"},
                {key: value for key, value in DECISION.items() if key not in {"actor", "agent_host"}},
                {**DECISION, "agent_host": "codex"},
            ):
                status, text = await patch(refused, "claude")
                assert status == 403 and "coding-agent process (claude)" in text, refused
    finally:
        await server.stop()


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
        ("PUT", "/api/reviews/rv-1/destination", True),
        # Snooze hides a Review from the person's own Your turn: theirs to decide (SPEC 7.1 P5-1 B).
        ("PUT", "/api/reviews/rv-1/queue-state", True),
        ("GET", "/api/reviews/rv-1/queue-state", False),
        ("POST", "/api/reviews/rv-1/flow-send", True),
        ("POST", "/api/reviews/rv-1/flow-send/op-1/undo", True),
        ("POST", "/api/reviews/rv-1/seen-coverage", True),
        ("GET", "/api/reviews/rv-1/flow-send", False),
        ("GET", "/api/reviews/rv-1/seen-coverage", False),
        # Agents legitimately report checks, refresh, ask and read.
        ("POST", "/api/reviews/rv-1/verification", False),
        ("POST", "/api/reviews/rv-1/refresh", False),
        ("POST", "/api/reviews/rv-1/ask", False),
        ("POST", "/api/reviews/rv-1/landing/cancel", False),
        ("GET", "/api/reviews/rv-1/marks", False),
        ("GET", "/api/reviews/rv-1/annotations", False),
        ("GET", "/api/reviews/rv-1/destination/options", False),
        ("PUT", "/api/reviews/rv-1/title", False),
    ],
)
def test_judgment_routes_are_the_human_only_writes(method: str, path: str, judgment: bool) -> None:
    assert is_judgment_request(method, path) is judgment


@pytest.mark.parametrize(
    "path",
    [
        "/api/reviews/rv-1/flow-send",
        "/api/reviews/rv-1/flow-send/op-1/undo",
        "/api/reviews/rv-1/seen-coverage",
    ],
)
def test_flow_writes_are_refused_for_an_agent_caller_and_allowed_for_a_human(path: str) -> None:
    assert is_judgment_request("POST", path)
    agent = {
        40: ProcessRecord(ppid=20, names=("python",), markers=()),
        20: ProcessRecord(ppid=1, names=("codex",), markers=()),
    }
    human = {
        40: ProcessRecord(ppid=10, names=("lc",), markers=()),
        10: ProcessRecord(ppid=1, names=("zsh",), markers=()),
    }
    # The server refuses whenever the host verdict is non-empty.
    assert agent_host_in_ancestry(40, agent.get) == "codex"
    assert agent_host_in_ancestry(40, human.get) == ""


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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="peer-process lookup is exercised on Linux")
def test_a_caller_that_closes_before_it_is_identified_is_not_trusted() -> None:
    # Send-and-close: a caller can write its whole request and let go of the socket
    # before the server looks it up. Its end then lingers ownerless or is gone.
    with socket.create_server(("127.0.0.1", 0)) as listener:
        client = socket.create_connection(listener.getsockname())
        accepted, _address = listener.accept()
        with accepted:
            ends = {"peername": accepted.getpeername(), "sockname": accepted.getsockname()}
            transport = SimpleNamespace(get_extra_info=ends.get)
            # While it waits for its answer the caller resolves: this process, no agent.
            assert peer_agent_host(transport) == ""
            client.close()
            assert peer_agent_host(transport) == UNIDENTIFIED_CALLER


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
        env = dict(os.environ)
        env.pop("LEMONCROW_REMOTE_MCP_SESSION", None)
        env["CLAUDECODE"] = "1"
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
