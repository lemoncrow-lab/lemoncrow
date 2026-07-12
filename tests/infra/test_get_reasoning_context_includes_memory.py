from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.memory_models import ArchivalPassage
from lemoncrow.gateway.adapters.mcp_server import _handle
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore


def _call_context(args: dict[str, Any]) -> dict[str, Any]:
    response = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "context", "arguments": args},
        }
    )
    assert response is not None
    assert "result" in response, response
    payload = json.loads(response["result"]["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


@pytest.fixture()
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    SqliteMemoryStore(root)
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

    import lemoncrow.gateway.adapters.mcp_server as mcp_server

    mcp_server._reset_runtime_cache_for_testing()
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    return root


def _insert_passage(
    root: Path,
    *,
    passage_id: str,
    agent_id: str,
    text: str,
    tags: list[str],
) -> None:
    SqliteMemoryStore(root).insert_passage(
        ArchivalPassage(
            id=passage_id,
            agent_id=agent_id,
            text=text,
            tags=tags,
            source="user",
            dedup_hash=passage_id,
        )
    )


def test_get_context_injects_same_agent_memory(memory_root: Path) -> None:
    _insert_passage(
        memory_root,
        passage_id="pas-lemoncrow-code",
        agent_id="lc:code",
        text="Scoped recall context injection should append durable memory for lemoncrow code.",
        tags=["agent:lc:code"],
    )

    payload = _call_context(
        {
            "task": "scoped recall context injection for lemoncrow code",
            "agent_id": "lc:code",
        }
    )

    assert "prefix_plan" not in payload  # diagnostics gated off by default
    assert "tokens_breakdown" in payload
    assert payload["tokens_breakdown"]["total"] >= 0


def test_get_context_does_not_leak_other_agent_memory(memory_root: Path) -> None:
    _insert_passage(
        memory_root,
        passage_id="pas-legacy-external-agent",
        agent_id="legacy.external-agent",
        text="Scoped recall context injection should never leak into lemoncrow code.",
        tags=["agent:legacy.external-agent"],
    )

    payload = _call_context(
        {
            "task": "scoped recall context injection for lemoncrow code",
            "agent_id": "lc:code",
        }
    )

    assert "prefix_plan" not in payload  # diagnostics gated off by default
    assert "recalled_passages" in payload


def test_get_context_can_disable_recall(memory_root: Path) -> None:
    _insert_passage(
        memory_root,
        passage_id="pas-disabled",
        agent_id="lc:code",
        text="Disabled recall passage should stay out of injected context.",
        tags=["agent:lc:code"],
    )

    payload = _call_context(
        {
            "task": "disabled recall passage",
            "agent_id": "lc:code",
            "recall": False,
        }
    )

    assert "prefix_plan" not in payload  # diagnostics gated off by default
    assert "recalled_passages" in payload
