from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from atelier.core.capabilities.grounded_loop import search_first


def test_search_first_returns_ranked_matches_and_explicit_follow_ups(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_CACHE_DISABLED", "1")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text(
        "class Playbook:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "beta.py").write_text(
        "from src.alpha import Playbook\n\nrb = Playbook()\n",
        encoding="utf-8",
    )

    payload = search_first(query="Playbook", task="trace Playbook", path=str(tmp_path))

    assert payload["discovery"]["tool"] == "search"
    assert payload["backend"] == "ripgrep"
    assert len(payload["matches"]) >= 2
    assert payload["calls_saved"] >= 1
    assert payload["handoff"]["read"]["tool"] == "read"
    assert payload["handoff"]["context"] == {
        "tool": "context",
        "mode": "symbols",
        "task": "trace Playbook",
        "files": [match["path"] for match in payload["matches"]],
    }
    assert payload["handoff"]["memory"] == {
        "tool": "context",
        "mode": "procedures",
        "task": "trace Playbook",
        "files": [match["path"] for match in payload["matches"]],
        "recall": True,
    }
    # `explore` is removed; its call-graph relations folded into grep, so the
    # search handoff points at grep's relation mode.
    assert payload["handoff"]["relations"] == {
        "tool": "grep",
        "relation": "usages",
        "symbol": "Playbook",
    }
    assert payload["matches"][0]["follow_up"]["read"] == {
        "tool": "read",
        "path": payload["matches"][0]["path"],
    }
    assert payload["matches"][0]["follow_up"]["context"] == {
        "tool": "context",
        "mode": "symbols",
        "task": "trace Playbook",
        "files": [payload["matches"][0]["path"]],
    }


def test_search_first_reuses_existing_search_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    search_first_module = importlib.import_module("atelier.core.capabilities.grounded_loop.search_first")
    fake_search = MagicMock(
        return_value={
            "matches": [{"path": "src/orders.py", "snippets": [{"text": "OrderService"}]}],
            "backend": "ripgrep",
            "cache_hit": False,
            "total_tokens": 42,
        }
    )
    monkeypatch.setattr(search_first_module, "smart_search", fake_search)

    payload = search_first(query="OrderService", task="inspect OrderService")

    fake_search.assert_called_once_with(
        query="OrderService",
        path=".",
        mode="chunks",
        max_files=8,
        max_chars_per_file=1600,
        include_outline=True,
        budget_tokens=2000,
        indexed_search=None,
    )
    assert payload["matches"][0]["path"] == "src/orders.py"
    assert payload["handoff"]["memory"]["tool"] == "context"
