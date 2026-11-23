"""Only the human may approve or deny a risky agent action on the loopback server (P12-AGENT).

The agent whose action waits holds the same machine token, so the decision
route is refused to a coding-agent process and, unlike other judgment, to a
caller the server could not identify (fail closed).
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
from lemoncrow_server_core import local_app
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_agent_guard import UNIDENTIFIED_CALLER, is_approval_decision, is_judgment_request

_POST = """
import sys, urllib.request, urllib.error
request = urllib.request.Request(
    sys.argv[1], data=(sys.argv[3] if len(sys.argv) > 3 else "{}").encode(), method="POST",
    headers={"Authorization": "Bearer " + sys.argv[2], "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.status); print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.code); print(exc.read().decode())
"""


@pytest.mark.parametrize(
    ("method", "path", "decision"),
    [
        ("POST", "/api/reviews/rv-1/agent-approvals/apr_1/decision", True),
        ("PUT", "/api/reviews/rv-1/agent-approvals/apr_1/decision", True),
        ("post", "/api/reviews/rv-1/agent-approvals/apr_1/decision/", True),
        ("POST", "/api/reviews/rv-1/agent-approvals/apr_1/x/decision", True),
        # The agent files requests and claims its own approval; reading is open.
        ("POST", "/api/reviews/rv-1/agent-approvals", False),
        ("POST", "/api/reviews/rv-1/agent-approvals/apr_1/claim", False),
        ("GET", "/api/reviews/rv-1/agent-approvals", False),
        ("GET", "/api/reviews/rv-1/agent-approvals/apr_1/decision", False),
    ],
)
def test_the_decision_route_is_the_human_only_approval_write(method: str, path: str, decision: bool) -> None:
    assert is_approval_decision(method, path) is decision
    # It is not ordinary judgment: the stricter guard owns it.
    assert not is_judgment_request(method, path) or not decision


async def _post(url: str, token: str, env: dict[str, str], body: str = "{}") -> tuple[str, str]:
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", _POST, url, token, body],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    status, body = result.stdout.split("\n", 1)
    return status, body


async def _exercise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", token_file=token_file, allow_local_fs=False)
    )
    token = token_file.read_text(encoding="utf-8").strip()
    url = await server.start()
    try:
        decision = f"{url}/api/reviews/rv-missing/agent-approvals/apr_1/decision"
        env = dict(os.environ)
        env.pop("LEMONCROW_REMOTE_MCP_SESSION", None)
        env["CLAUDECODE"] = "1"
        status, body = await _post(decision, token, env)
        assert status == "403", body
        assert "coding-agent process (claude)" in json.loads(body)["error"]["message"]
        # The same agent may still file and claim its own requests.
        filed, _body = await _post(f"{url}/api/reviews/rv-missing/agent-approvals", token, env)
        claim, _body = await _post(f"{url}/api/reviews/rv-missing/agent-approvals/apr_1/claim", token, env)
        assert filed != "403" and claim != "403", (filed, claim)

        headers = {"Authorization": f"Bearer {token}"}
        async with ClientSession() as client:
            # A resolved non-agent caller passes the guard and gets the Review API's own answer.
            own = await client.post(decision, json={}, headers=headers)
            assert own.status != 403, await own.text()
            # An unresolved or vanished caller is refused, never guessed human.
            for unresolved in (None, UNIDENTIFIED_CALLER):
                monkeypatch.setattr(local_app, "peer_agent_host", lambda _transport, value=unresolved: value)
                refused = await client.post(decision, json={}, headers=headers)
                assert refused.status == 403, unresolved
                message = (await refused.json())["error"]["message"]
                if unresolved is None:
                    assert "could not be identified" in message
                else:
                    assert "approving or denying an agent action cannot come from" in message
                # Ordinary judgment keeps its existing rule for the same caller.
                marks = await client.post(f"{url}/api/reviews/rv-missing/marks", json={}, headers=headers)
                if unresolved is None:
                    assert marks.status != 403, await marks.text()
    finally:
        await server.stop()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="peer-process lookup is exercised on Linux")
def test_an_agent_process_or_unidentified_caller_cannot_decide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_exercise(tmp_path, monkeypatch))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def _exercise_local_flow(tmp_path: Path) -> None:
    from lemoncrow_client.config import ClientConfig

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.hosted import capture_server_review

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    (repo / "README.md").write_text("two\n", encoding="utf-8")

    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", token_file=token_file, allow_local_fs=True)
    )
    token = token_file.read_text(encoding="utf-8").strip()
    url = await server.start()
    config = ClientConfig(
        url=url,
        token=token,
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )
    try:
        captured = await asyncio.to_thread(
            capture_server_review,
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=200,
            with_impact=False,
            with_provenance=False,
        )
        base = f"{url}/api/reviews/{captured.review_id}/agent-approvals"
        env = dict(os.environ)
        env.pop("LEMONCROW_REMOTE_MCP_SESSION", None)
        env["CLAUDECODE"] = "1"
        identity = {"host": "claude", "session_id": "author", "kind": "command", "action": "git push origin main"}
        # The agent's own process files the request (as its hook does).
        status, body = await _post(base, token, env, json.dumps({**identity, "classes": ["git_push"]}))
        assert status == "200", body
        approval = json.loads(body)["approval"]
        decision = f"{base}/{approval['id']}/decision"
        verdict = json.dumps({"decision": "approve", "expected_action_digest": approval["action_digest"]})
        status, body = await _post(decision, token, env, verdict)
        assert status == "403", body
        # The human (a resolved non-agent caller) decides; the agent then claims it once.
        async with ClientSession() as client:
            decided = await client.post(decision, data=verdict, headers={"Authorization": f"Bearer {token}"})
            assert decided.status == 200, await decided.text()
            assert (await decided.json())["approval"]["status"] == "approved"
        status, body = await _post(f"{base}/{approval['id']}/claim", token, env, json.dumps(identity))
        assert status == "200" and json.loads(body)["granted"] is True, body
    finally:
        await server.stop()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="peer-process lookup is exercised on Linux")
def test_local_flow_agent_requests_human_decides_agent_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "lemoncrow-root"))
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    asyncio.run(_exercise_local_flow(tmp_path))


@pytest.mark.parametrize(
    ("method", "path", "judgment"),
    [
        ("PATCH", "/api/review-settings", True),
        ("DELETE", "/api/review-settings", True),
        ("GET", "/api/review-settings", False),
        ("PUT", "/api/review-intelligence/settings", True),
        ("GET", "/api/review-intelligence/settings", False),
        ("PATCH", "/api/review-settings/other", False),
    ],
)
def test_an_agent_cannot_change_the_reviewers_settings(method: str, path: str, judgment: bool) -> None:
    assert is_judgment_request(method, path) is judgment
