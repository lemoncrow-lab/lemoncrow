"""Tests for memory interoperability wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from lemoncrow.infra.memory_bridges.letta_adapter import LettaAdapter
from lemoncrow.infra.memory_bridges.openmemory import OpenMemoryAdapter, OpenMemoryMemoryStore
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore


class _FakeOpenMemoryClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "add_memories":
            row = {
                "id": f"mem-{len(self.rows) + 1}",
                "memory": arguments["messages"][0]["content"],
                "metadata": dict(arguments.get("metadata") or {}),
            }
            self.rows.append(row)
            return {"results": [row]}
        if name == "search_memory":
            query = str(arguments["query"]).lower()
            return [
                row
                for row in self.rows
                if query in row["memory"].lower() or query in str(row.get("metadata", {})).lower()
            ]
        if name == "list_memories":
            return list(self.rows)
        raise AssertionError(f"unexpected tool call: {name}")


class _FakeEmptyOpenMemoryClient:
    base_url = "http://127.0.0.1:8765"
    timeout = 5

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        _ = (name, arguments)
        return {}


def test_openmemory_adapter_always_delegates_to_bridge() -> None:
    adapter = OpenMemoryAdapter(client=_FakeOpenMemoryClient())
    result = adapter.fetch_context(task="Fix the checkout bug")

    assert result.ok is True
    assert result.source == "openmemory"


def test_openmemory_memory_store_uses_openmemory_as_primary(tmp_path: Path) -> None:
    store = OpenMemoryMemoryStore(tmp_path / "lemoncrow", client=_FakeOpenMemoryClient())

    block = store.upsert_block(MemoryBlock(agent_id="lemon:code", label="style", value="compact"), actor="tests")
    assert store.get_block("lemon:code", "style") == block

    passage = store.insert_passage(
        ArchivalPassage(
            agent_id="lemon:code",
            text="checkout retry guidance",
            tags=["checkout"],
            source="trace",
            source_ref="trace-1",
            dedup_hash="hash-1",
        )
    )
    results = store.search_passages("lemon:code", "checkout", top_k=1)
    assert [item.id for item in results] == [passage.id]

    sqlite = SqliteMemoryStore(tmp_path / "lemoncrow")
    assert sqlite.get_block("lemon:code", "style") is None
    assert sqlite.list_passages("lemon:code") == []


def test_openmemory_adapter_rest_fallback_when_mcp_returns_empty(monkeypatch: Any) -> None:
    adapter = OpenMemoryAdapter(client=_FakeEmptyOpenMemoryClient())

    class _Resp:
        def __init__(self, payload: str) -> None:
            self._payload = payload.encode("utf-8")

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = (exc_type, exc, tb)
            return None

    def _fake_urlopen(request: Any, timeout: int) -> _Resp:
        _ = timeout
        url = request.full_url
        method = request.get_method()
        if method == "POST" and url.endswith("/api/v1/memories/"):
            return _Resp('{"id":"m1","content":"ok"}')
        if method == "GET" and "/api/v1/memories/" in url and "search_query=checkout" in url:
            return _Resp('{"items":[{"id":"m2","content":"checkout retry"}]}')
        if method == "GET" and "/api/v1/memories/" in url:
            return _Resp('{"items":[{"id":"m3","content":"all rows"}]}')
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    added = adapter.add_memory(text="checkout", user_id="pankaj", metadata={})
    assert added["id"] == "m1"
    searched = adapter.search_memories(query="checkout", user_id="pankaj", limit=5)
    assert searched and searched[0]["id"] == "m2"
    listed = adapter.list_memories(user_id="pankaj", limit=5)
    assert listed and listed[0]["id"] == "m3"


def test_openmemory_row_to_passage_accepts_metadata_fallback_shape() -> None:
    row = {
        "id": "mem-1",
        "content": "[run][fact-001] database durability improves reliability",
        "created_at": 1779450000,
        "categories": ["run", "fact-001"],
        "metadata_": {
            "lemoncrow_kind": "lemoncrow_passage",
            "lemoncrow_agent_id": "agent-1",
            "lemoncrow_passage_id": "pas-1",
            "lemoncrow_dedup_hash": "hash-1",
            "lemoncrow_source": "user",
            "lemoncrow_source_ref": "run",
        },
    }
    passage = OpenMemoryMemoryStore._row_to_passage(row, agent_id="agent-1")
    assert passage is not None
    assert passage.id == "pas-1"
    assert passage.dedup_hash == "hash-1"
    assert passage.text.startswith("[run][fact-001]")
    assert "fact-001" in passage.tags


def test_openmemory_search_memories_ranks_broad_rows_when_direct_search_empty(monkeypatch: Any) -> None:
    adapter = OpenMemoryAdapter(client=_FakeEmptyOpenMemoryClient())

    class _Resp:
        def __init__(self, payload: str) -> None:
            self._payload = payload.encode("utf-8")

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = (exc_type, exc, tb)
            return None

    def _fake_urlopen(request: Any, timeout: int) -> _Resp:
        _ = timeout
        url = request.full_url
        method = request.get_method()
        if method == "GET" and "search_query=how+can+we+make+db+durability+better" in url:
            return _Resp('{"items":[]}')
        if method == "GET" and "search_query=" not in url:
            return _Resp(
                '{"items":['
                '{"id":"x1","content":"database durability improves reliability under load scenario 9","metadata_":{"k":"v"}},'
                '{"id":"x2","content":"queue dead letters processing details","metadata_":{"k":"v"}}'
                "]}",
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    rows = adapter.search_memories(
        query="how can we make db durability better",
        user_id="pankaj",
        limit=1,
    )
    assert rows and rows[0]["id"] == "x1"


def test_letta_adapter_extracts_results_shape_and_content_fields() -> None:
    class _SearchResponse:
        def model_dump(self) -> dict[str, Any]:
            return {
                "count": 1,
                "results": [
                    {
                        "id": "passage-1",
                        "content": "retrieval reranking improves relevance",
                        "timestamp": "2026-05-22T10:52:57.409958+00:00",
                        "tags": ["quality"],
                    }
                ],
            }

    items = LettaAdapter._extract_items(_SearchResponse())
    assert len(items) == 1
    passage = LettaAdapter.letta_to_passage(items[0], agent_id="agent-1")
    assert passage is not None
    assert passage.text == "retrieval reranking improves relevance"
    assert passage.id == "passage-1"
