"""The real thin client against bootstrap.build_server's default dispatcher.

No warmed public index and no test-only IndexBackedDispatcher: the first MCP
search after upload or edit must see the same bytes as the direct index API.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _serverpkg import REASON, server_available
from lemoncrow_client.config import load_config
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.session import RemoteSession

pytestmark = [
    pytest.mark.enterprise_server,
    pytest.mark.skipif(not server_available(), reason=REASON),
]


@pytest.fixture(params=["memory", "sqlite"])
def shipped(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    from lemoncrow_server.auth import token_digest
    from lemoncrow_server.bootstrap import build_server
    from lemoncrow_server.ops.bringup import prepare
    from test_end_to_end_server import _Loop

    evaluation = prepare(tmp_path / "server", port=0, roles=("developer",))
    tokens = {
        "alice": evaluation.token,
        "bob": "lc_shipped_search_bob_0123456789",
        "mallory": "lc_shipped_search_mallory_012345",
    }
    records = json.loads(evaluation.token_path.read_text())
    for name in ("bob", "mallory"):
        records["tokens"].append(
            {
                "token_id": "tok_" + name,
                "token_sha256": token_digest(tokens[name]),
                "subject": "usr_" + name,
                "org_id": evaluation.org_id if name == "bob" else "org_other",
                "roles": ["developer"],
            }
        )
    evaluation.token_path.write_text(json.dumps(records))
    config = replace(
        evaluation.config,
        org_ids=(evaluation.org_id, "org_other"),
        index_path=evaluation.index_path if request.param == "sqlite" else None,
    )
    server, _state, _app = build_server(config)
    loop = _Loop()
    url = loop.run(server.start())
    sessions: list[RemoteSession] = []

    def connect(name: str, files: Mapping[str, str]) -> tuple[RemoteSession, Dispatcher, Path]:
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for path, body in files.items():
            destination = repo / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(body)
        client_config = load_config(
            {
                "LEMONCROW_URL": url,
                "LEMONCROW_TOKEN": tokens[name],
                "LEMONCROW_HOME": str(tmp_path / (name + "-client")),
                "LEMONCROW_STARTUP_BUDGET_S": "30",
            },
            cwd=repo,
        )
        session = RemoteSession(client_config)
        sessions.append(session)
        report = session.bootstrap(deadline_s=30)
        assert report.ok, report.reason
        return session, Dispatcher(client_config, session), repo

    try:
        yield connect
    finally:
        for session in sessions:
            session.close()
        loop.run(server.stop())
        loop.close()
        if _state.workspace is not None:
            _state.workspace.close()


def _text(dispatcher: Dispatcher, query: str, **arguments: Any) -> str:
    result = dispatcher.call("code_search", {"query": query, **arguments})
    assert not result.is_error, result.content
    assert not result.degraded, result.content
    return "\n".join(str(block.get("text", "")) for block in result.content)


def test_first_mcp_search_sees_upload_edit_rename_and_deletion(shipped: Any) -> None:
    session, dispatcher, repo = shipped("alice", {"hello.py": "def marker_before():\n    return 7\n"})
    mcp = McpServer(dispatcher._context.config, session)
    first = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "code_search", "arguments": {"query": "marker_before"}},
        }
    )
    assert first is not None and not first["result"]["isError"]
    assert "def marker_before" in str(first["result"]["content"])

    edited = dispatcher.call(
        "edit",
        {
            "edits": [
                {"path": "hello.py", "old": "marker_before", "new": "marker_after"},
            ]
        },
    )
    assert not edited.is_error, edited.content
    assert "def marker_after" in _text(dispatcher, "marker_after")
    assert _text(dispatcher, "marker_before") == "No matches in this view."
    assert session.query_index("symbol", symbol="marker_after")["hits"][0]["path"] == "hello.py"

    (repo / "hello.py").rename(repo / "renamed.py")
    session.push_paths(("hello.py", "renamed.py"))
    renamed = _text(dispatcher, "marker_after")
    assert "renamed.py" in renamed and "hello.py" not in renamed
    (repo / "renamed.py").unlink()
    session.push_paths(("renamed.py",))
    assert _text(dispatcher, "marker_after") == "No matches in this view."


def test_broad_query_ranks_partial_term_matches_instead_of_returning_empty(shipped: Any) -> None:
    _session, dispatcher, _repo = shipped(
        "alice",
        {
            "plugin.py": "codex_apps = {'lc_lc': 'registered'}\n",
            "launcher.py": "def launch_mcp_server():\n    return 'Internal error -32603'\n",
        },
    )

    result = _text(
        dispatcher,
        "codex_apps lc_lc MCP plugin server registration manifest launch command Internal error -32603",
        limit=8,
    )

    assert "plugin.py" in result
    assert "launcher.py" in result


def test_search_never_uses_another_user_or_organization_view(shipped: Any) -> None:
    alice, a, _repo = shipped("alice", {"private.py": "def alice_secret_marker():\n    return 1\n"})
    assert "private.py" in _text(a, "alice_secret_marker")
    for name in ("bob", "mallory"):
        other, dispatcher, _repo = shipped(name, {"own.py": "value = 2\n"})
        assert _text(dispatcher, "alice_secret_marker") == "No matches in this view."
        other.state.view_id = alice.state.view_id
        other.state.view_revision = alice.view_revision
        with pytest.raises(ClientError) as caught:
            other.call_tool("code_search", {"query": "alice_secret_marker"})
        assert caught.value.code is ErrorCode.VIEW_UNKNOWN


def test_scope_ranks_before_limit_and_regex_aliases_stay_usable(shipped: Any) -> None:
    files = {f"p{number:02}.py": "def common_marker():\n    return 1\n" for number in range(40)}
    _session, dispatcher, _repo = shipped("alice", files)
    scoped = _text(dispatcher, "common_marker", paths=["p39.py"], limit=1)
    assert "p39.py" in scoped and "p00.py" not in scoped
    assert "results truncated" in scoped
    # Scope remains a preference: the rest of the view is still searchable.
    assert "p00.py" in _text(dispatcher, "common_marker", paths="p39.py", limit=2)
    regex_result = dispatcher.call(
        "code_search",
        {
            "regex": r"def common_mark[e]r\(",
            "max_results": 1,
            "include_paths": ["p39.py"],
        },
    )
    assert not regex_result.is_error, regex_result.content
    assert "p39.py" in str(regex_result.content)
    assert "p00.py" in _text(dispatcher, "p00.py")


@pytest.mark.parametrize("limit", [0, 33, True])
def test_invalid_limit_is_refused(shipped: Any, limit: Any) -> None:
    _session, dispatcher, _repo = shipped("alice", {"a.py": "marker = 1\n"})
    result = dispatcher.call("code_search", {"query": "marker", "limit": limit})
    assert result.is_error


def test_limit_boundary_and_missing_view(shipped: Any) -> None:
    _session, dispatcher, _repo = shipped("alice", {f"p{number:02}.py": "marker = 1\n" for number in range(33)})
    result = _text(dispatcher, "marker", limit=32)
    assert "p31.py" in result and "p32.py" not in result and "results truncated" in result
    unbound = RemoteSession(dispatcher._context.config)
    try:
        unbound.open_session()
        with pytest.raises(ClientError) as caught:
            unbound.call_tool("code_search", {"query": "marker"})
        assert caught.value.code is ErrorCode.VIEW_UNKNOWN
    finally:
        unbound.close()


def test_regex_errors_and_expensive_queries_are_bounded(shipped: Any) -> None:
    _session, dispatcher, _repo = shipped("alice", {"a.txt": "a" * 30000 + "!"})
    invalid = dispatcher.call("code_search", {"query": "[unterminated"})
    assert invalid.is_error and "payload_invalid" in str(invalid.content)
    expensive = dispatcher.call("code_search", {"query": "(a|aa)+$"})
    assert expensive.is_error and "deadline_exceeded" in str(expensive.content)


def test_empty_files_and_unacknowledged_revisions(shipped: Any) -> None:
    session, dispatcher, _repo = shipped("alice", {"empty.py": ""})
    assert "empty.py (empty file)" in _text(dispatcher, "empty.py")
    session.state.view_revision += 1
    with pytest.raises(ClientError) as caught:
        session.call_tool("code_search", {"query": "empty.py"})
    assert caught.value.code is ErrorCode.VIEW_REVISION_UNACKNOWLEDGED
    session.state.view_revision -= 1
