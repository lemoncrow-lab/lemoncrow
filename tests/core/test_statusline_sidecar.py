"""Structured statusline sidecar written for rich frontends."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.statusline_sidecar import (
    STATUS_FILE_ENV,
    StatusSnapshot,
    configured_status_path,
    read_status_snapshot,
    status_file_path,
    write_status_snapshot,
)


def _turn(snapshot: StatusSnapshot, **overrides: float | int | str) -> None:
    payload: dict[str, float | int | str] = {
        "provider": "zen",
        "model": "zen/big-pickle",
        "input_tokens": 100,
        "cache_read_tokens": 50,
        "cache_write_tokens": 10,
        "output_tokens": 20,
        "cost_usd": 0.01,
        "saved_usd": 0.02,
        "cache_efficiency_pct": 40.0,
    }
    payload.update(overrides)
    snapshot.add_turn(**payload)  # type: ignore[arg-type]


def test_turns_accumulate() -> None:
    snapshot = StatusSnapshot()
    _turn(snapshot)
    _turn(snapshot, cache_efficiency_pct=60.0)
    assert snapshot.turns == 2
    assert snapshot.input_tokens == 200
    assert snapshot.output_tokens == 40
    assert snapshot.cost_usd == pytest.approx(0.02)
    assert snapshot.saved_usd == pytest.approx(0.04)
    assert snapshot.context_tokens == 320
    # Efficiency is a point-in-time ratio, not a sum.
    assert snapshot.cache_efficiency_pct == 60.0


def test_negative_usage_is_clamped() -> None:
    snapshot = StatusSnapshot()
    _turn(snapshot, input_tokens=-5, cost_usd=-1.0)
    assert snapshot.input_tokens == 0
    assert snapshot.cost_usd == 0.0


def test_tool_calls_counted_and_mcp_split() -> None:
    snapshot = StatusSnapshot()
    snapshot.record_tool_call("read")
    snapshot.record_tool_call("read")
    snapshot.record_tool_call("mcp__github__list_issues")
    snapshot.record_tool_call("mcp_tool")
    snapshot.record_tool_call("")
    assert snapshot.tool_calls == {"read": 2, "mcp__github__list_issues": 1, "mcp_tool": 1}
    assert snapshot.tool_call_total == 4
    assert snapshot.mcp_calls == 2


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = status_file_path(tmp_path, "code-1")
    snapshot = StatusSnapshot()
    _turn(snapshot)
    snapshot.record_tool_call("edit")
    write_status_snapshot(path, snapshot)

    payload = read_status_snapshot(path)
    assert payload is not None
    assert payload["model"] == "zen/big-pickle"
    assert payload["context_tokens"] == 160
    assert payload["tool_call_total"] == 1
    assert payload["updated_at"] > 0
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_write_leaves_no_temp_files(tmp_path: Path) -> None:
    path = status_file_path(tmp_path, "code-1")
    write_status_snapshot(path, StatusSnapshot())
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_read_of_missing_or_corrupt_file_returns_none(tmp_path: Path) -> None:
    assert read_status_snapshot(tmp_path / "nope.json") is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_status_snapshot(corrupt) is None


def test_configured_path_follows_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(STATUS_FILE_ENV, raising=False)
    assert configured_status_path() is None
    monkeypatch.setenv(STATUS_FILE_ENV, str(tmp_path / "status.json"))
    assert configured_status_path() == tmp_path / "status.json"
