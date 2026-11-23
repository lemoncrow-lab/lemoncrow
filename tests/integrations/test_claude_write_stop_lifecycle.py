"""Exact native Write deltas and idempotent per-turn Stop snapshots."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from integrations.claude.plugin.hooks import post_tool_use, pre_write_snapshot, stop
from lemoncrow.core.foundation.paths import session_dir


def _hook(module, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert module.main() == 0


def _edit_events(root: Path) -> list[dict]:
    path = session_dir(root, "claude", "sid") / "run.json"
    if not path.is_file():
        return []
    return [event for event in json.loads(path.read_text(encoding="utf-8"))["events"] if event["kind"] == "file_edit"]


def test_native_write_attributes_only_its_own_delta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lc"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(post_tool_use, "_nudge_lemoncrow_view", lambda *args: None)
    monkeypatch.setattr(post_tool_use, "_jj_snapshot_nudge", lambda: None)

    from lemoncrow.pro.capabilities.review import live_runtime

    monkeypatch.setattr(live_runtime, "request_live_review_prepare", lambda *args, **kwargs: None)
    target = workspace / "document.txt"
    target.write_text("baseline\n", encoding="utf-8")

    def write(text: str, tool_id: str) -> None:
        payload = {
            "tool_name": "Write",
            "session_id": "sid",
            "tool_use_id": tool_id,
            "tool_input": {"file_path": str(target)},
            "cwd": str(workspace),
        }
        _hook(pre_write_snapshot, monkeypatch, payload)
        target.write_text(text, encoding="utf-8")
        _hook(post_tool_use, monkeypatch, payload)

    write("baseline\nfirst edit\n", "write-1")
    write("baseline\nfirst edit\nsecond edit\n", "write-2")
    events = _edit_events(root)
    assert len(events) == 2
    first, second = [event["payload"]["diff"] for event in events]
    assert "+first edit" in first
    assert "+second edit" in second
    assert "+first edit" not in second


def test_missing_write_preimage_never_claims_cumulative_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lc"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "document.txt"
    target.write_text("already edited\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(post_tool_use, "_nudge_lemoncrow_view", lambda *args: None)
    monkeypatch.setattr(post_tool_use, "_jj_snapshot_nudge", lambda: None)
    _hook(
        post_tool_use,
        monkeypatch,
        {
            "tool_name": "Write",
            "session_id": "sid",
            "tool_use_id": "unknown",
            "tool_input": {"file_path": str(target)},
            "cwd": str(workspace),
        },
    )
    assert not _edit_events(root)


def test_failed_write_discards_sensitive_preimage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.foundation.write_preimage import _snapshot_path

    root = tmp_path / "lc"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    target = workspace / "secret.txt"
    target.write_text("sensitive prewrite text", encoding="utf-8")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "session_id": "sid",
        "tool_use_id": "failed-write",
        "tool_input": {"file_path": str(target)},
        "cwd": str(workspace),
    }
    _hook(pre_write_snapshot, monkeypatch, payload)
    assert _snapshot_path(root, "sid", "failed-write").exists()
    _hook(pre_write_snapshot, monkeypatch, {**payload, "hook_event_name": "PostToolUseFailure"})
    assert not _snapshot_path(root, "sid", "failed-write").exists()


def test_stop_rows_are_cumulative_not_duplicate_charges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    root = session_dir(tmp_path, "claude", "sid")
    root.mkdir(parents=True)
    (root / "savings.jsonl").write_text('{"kind":"read","tokens":42}\n', encoding="utf-8")
    stop._write_session_cost("sid", 4.0, 1000, carry_usd=1.0, carry_tokens=25)
    stop._write_session_cost("sid", 4.0, 1000, carry_usd=1.0, carry_tokens=25)
    stop._write_session_cost("sid", 5.0, 1100, carry_usd=1.0, carry_tokens=25)
    rows = [json.loads(line) for line in (root / "savings.jsonl").read_text().splitlines()]
    costs = [row for row in rows if row.get("kind") == "session_end"]
    assert len(costs) == 2
    assert costs[-1]["est_cost_usd"] == 5.0


def test_stop_run_event_not_duplicated_when_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    root = session_dir(tmp_path, "claude", "sid")
    root.mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({"session_id": "sid", "events": []}), encoding="utf-8")
    stats = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_tokens": 1200,
        "est_cost_usd": 0.05,
        "tool_calls": 3,
        "tools_used": {"mcp__lc__read": 3},
    }
    stop._write_token_event(stats, "sid")
    stop._write_token_event(stats, "sid")
    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert len(data["events"]) == 1
    assert "turn summary" in data["events"][0]["summary"]
    stats["tool_calls"] = 4
    stop._write_token_event(stats, "sid")
    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert len(data["events"]) == 2
