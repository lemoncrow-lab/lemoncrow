from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp_server import TOOLS, _handle
from lemoncrow.infra.storage.memory_store import MemorySidecarUnavailable


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    response = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    assert response is not None
    assert isinstance(response, dict)
    return response


def _payload(response: dict[str, Any]) -> Any:
    assert "result" in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _memory_args(op: str, **kwargs: Any) -> dict[str, Any]:
    return {"op": op, **kwargs}


@pytest.fixture()
def mcp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setattr(mcp_server, "_REMOTE_TOOLS", frozenset())
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    return root


def test_memory_tools_are_registered() -> None:
    assert "memory" in TOOLS


def test_memory_schema_exposes_recall_store_vote_and_symbol_ops() -> None:
    memory_tool = TOOLS["memory"]
    props = memory_tool["inputSchema"]["properties"]
    ops = set(props["op"]["enum"])

    assert ops == {"recall", "recall_symbol", "store_fact", "vote_fact"}
    assert "block_upsert" not in props["op"]["description"]
    assert "transcript_recall" not in props["op"]["description"]
    assert "summarize" not in props["op"]["description"]
    assert "query" in props
    assert "subject" in props
    assert "fact" in props
    assert "citations" in props
    assert "reason" in props
    assert "scope" in props
    assert "direction" in props
    assert "label" not in props
    assert "value" not in props
    assert "text" not in props
    assert "source" not in props
    assert "session_id" not in props
    assert "metadata" not in props


def test_memory_store_fact_and_vote_fact_round_trip(mcp_root: Path) -> None:
    _ = mcp_root
    stored = _payload(
        _call(
            "memory",
            _memory_args(
                "store_fact",
                agent_id="lemon:code",
                subject="workflow preference",
                fact="Prefer LemonCrow memory over host memory by default.",
                citations='User input: "prefer LemonCrow"',
                reason="Ensures local memory stays source of truth.",
                scope="user",
            ),
        )
    )
    assert stored["fact"] == "Prefer LemonCrow memory over host memory by default."
    assert stored["scope"] == "user"
    assert "votes" not in stored

    voted = _payload(
        _call(
            "memory",
            _memory_args(
                "vote_fact",
                agent_id="lemon:code",
                fact="Prefer LemonCrow memory over host memory by default.",
                direction="upvote",
                reason="Verified and useful across tasks.",
                scope="user",
            ),
        )
    )
    assert voted["direction"] == "upvote"
    assert "votes" not in voted

    stored_again = _payload(
        _call(
            "memory",
            _memory_args(
                "store_fact",
                agent_id="lemon:code",
                subject="workflow preference",
                fact="Prefer LemonCrow memory over host memory by default.",
                citations='User input: "prefer LemonCrow"',
                reason="Ensures local memory stays source of truth.",
                scope="user",
            ),
        )
    )
    assert stored_again["id"] == stored["id"]
    assert "votes" not in stored_again


def test_memory_recall_renders_compact_markdown(mcp_root: Path) -> None:
    _ = mcp_root
    response = _call("memory", _memory_args("recall", query="nonexistent-query", top_k=2))
    text = response["result"]["content"][0]["text"]
    # recall now renders compact markdown (header + per-passage blocks) instead
    # of a JSON passage list; an empty recall shows the seeding hint.
    assert text.startswith("### memory")
    assert "no passages" in text


def test_memory_rejects_removed_ops(mcp_root: Path) -> None:
    _ = mcp_root
    for op in ("block_upsert", "block_get", "archive", "transcript_recall", "summarize"):
        response = _call("memory", _memory_args(op, query="x"))
        assert "error" in response
        assert response["error"]["code"] in {-32602, -32000}


def test_memory_recall_symbol_dispatches_to_local_capability(monkeypatch: pytest.MonkeyPatch, mcp_root: Path) -> None:
    _ = mcp_root
    captured: dict[str, Any] = {}

    class FakeRecall:
        def recall_symbol(self, *, query: str, agent_id: str | None, top_k: int) -> dict[str, Any]:
            captured.update(query=query, agent_id=agent_id, top_k=top_k)
            return {"query": query, "definition": {"qualified_name": query}, "included": ["definition", "memory"]}

    monkeypatch.setattr(mcp_server, "_symbol_recall", lambda: FakeRecall())
    response = _call("memory", _memory_args("recall_symbol", query="AuthService.verify_session", top_k=3))
    assert "result" in response, response
    assert captured == {"query": "AuthService.verify_session", "agent_id": None, "top_k": 3}


def test_memory_sidecar_unavailable_maps_to_503(monkeypatch: pytest.MonkeyPatch, mcp_root: Path) -> None:
    _ = mcp_root

    class DownStore:
        def list_blocks(self, *args: Any, **kwargs: Any) -> list[Any]:
            _ = (args, kwargs)
            raise MemorySidecarUnavailable("sidecar down")

    monkeypatch.setattr(mcp_server, "_memory_store", lambda: DownStore())
    response = _call(
        "memory",
        _memory_args(
            "store_fact",
            agent_id="lemon:code",
            subject="workflow",
            fact="test fact",
            citations='User input: "x"',
            reason="x",
            scope="user",
        ),
    )
    assert response["error"]["code"] == 503
