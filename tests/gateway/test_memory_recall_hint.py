"""``memory(op=recall)`` hint + two-store merge regression.

Finding #1: the SessionStart indexer writes past-session passages to
``recall.db`` while the model-facing recall tool read only ``memory.db``, so
indexed content was invisible. ``_memory_recall`` now also reads the
session-recall store and folds its hits into ``passages``. These tests stub the
session-recall side so the hint/merge assertions stay deterministic regardless
of any real ``recall.db`` on the host.
"""

from __future__ import annotations

from typing import Any

import pytest

from atelier.gateway.adapters import mcp_server


class _FakeRecall:
    def __init__(self, passages: list[dict[str, Any]]) -> None:
        self._passages = passages

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"passages": self._passages}


class _FakeService:
    def __init__(self, passages: list[dict[str, Any]]) -> None:
        self._passages = passages

    def recall(self, **_kwargs: Any) -> _FakeRecall:
        return _FakeRecall(self._passages)


def test_empty_recall_gets_helpful_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_memory_service", lambda: _FakeService([]))
    monkeypatch.setattr(mcp_server, "_session_recall_passages", lambda *a, **k: [])
    out = mcp_server._memory_recall(None, "anything")
    assert "hint" in out
    assert "store_fact" in out["hint"]


def test_nonempty_recall_has_no_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_memory_service", lambda: _FakeService([{"text": "x"}]))
    monkeypatch.setattr(mcp_server, "_session_recall_passages", lambda *a, **k: [])
    out = mcp_server._memory_recall(None, "anything")
    assert "hint" not in out
    assert out["passages"] == [{"text": "x"}]


def test_recall_folds_in_session_passages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past-session hits from recall.db surface through memory(op=recall)."""
    from atelier.core.capabilities import session_recall

    monkeypatch.setattr(mcp_server, "_memory_service", lambda: _FakeService([]))
    monkeypatch.setattr(mcp_server, "_atelier_root", lambda: "/tmp/does-not-matter")
    monkeypatch.setattr(
        session_recall,
        "recall",
        lambda *a, **k: [
            {
                "text": "harbor run terminal-bench",
                "session": "sess-1",
                "tags": ["session-recall", "agent:any"],
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    )
    out = mcp_server._memory_recall(None, "deploy command")
    assert "hint" not in out
    assert out["passages"] == [
        {
            "id": "sess-1",
            "text": "harbor run terminal-bench",
            "source_ref": "sess-1",
            "tags": ["session-recall", "agent:any"],
        }
    ]


def test_recall_caps_merged_passages_to_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF#1: merged memory+session passages must not exceed top_k, and session
    hits must stay visible within that budget."""
    monkeypatch.setattr(
        mcp_server,
        "_memory_service",
        lambda: _FakeService([{"text": f"m{i}", "source_ref": f"m{i}"} for i in range(5)]),
    )
    monkeypatch.setattr(mcp_server, "_atelier_root", lambda: "/tmp/does-not-matter")
    from atelier.core.capabilities import session_recall

    monkeypatch.setattr(
        session_recall,
        "recall",
        lambda *a, **k: [
            {"text": f"s{i}", "session": f"s{i}", "tags": [], "created_at": "2026-01-01T00:00:00+00:00"}
            for i in range(3)
        ],
    )
    out = mcp_server._memory_recall(None, "q", top_k=5)
    assert len(out["passages"]) <= 5
    assert any(
        str(p.get("source_ref", "")).startswith("s") for p in out["passages"]
    ), "past-session hits should remain visible within the top_k budget"
