"""Tests for the host-aware live-session context probe."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.hosts import context_state as cs


def test_unknown_host_returns_zero() -> None:
    assert cs.host_context_state("mystery-host", "s1") == (0, "")


def test_empty_session_id_returns_zero() -> None:
    assert cs.host_context_state("claude", "") == (0, "")


def test_claude_dispatches_to_core_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.capabilities import savings_summary as ss

    monkeypatch.setattr(ss, "transcript_context_state", lambda sid: (123_000, "claude-sonnet-4-5"))
    assert cs.host_context_state("claude", "s1") == (123_000, "claude-sonnet-4-5")


def _write_codex_session(root: Path, session_id: str, lines: list[dict[str, Any]]) -> None:
    d = root / "2026" / "06" / "10"
    d.mkdir(parents=True)
    p = d / f"rollout-2026-06-10T12-00-00-{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_codex_probe_openai_style_usage_uses_live_used_tokens(tmp_path: Path) -> None:
    # OpenAI semantics: input_tokens is cumulative billing data and includes
    # cached_input_tokens. Codex's UI "used" value is the uncached remainder.
    _write_codex_session(
        tmp_path,
        "abc-1234",
        [
            {"type": "session_meta", "payload": {"id": "abc-1234"}},
            {
                "model": "gpt-5-codex",
                "usage": {
                    "input_tokens": 120_000,
                    "output_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 100_000},
                },
            },
        ],
    )
    ctx, model = cs._codex_probe("abc-1234", root=tmp_path)
    assert ctx == 20_000
    assert model == "gpt-5-codex"


def test_codex_probe_openai_style_usage_includes_cache_writes(tmp_path: Path) -> None:
    _write_codex_session(
        tmp_path,
        "abc-write",
        [
            {
                "usage": {
                    "input_tokens": 120_000,
                    "cached_input_tokens": 100_000,
                    "cache_write_tokens": 2_000,
                },
            },
        ],
    )

    ctx, _model = cs._codex_probe("abc-write", root=tmp_path)
    assert ctx == 22_000


def test_codex_probe_split_cache_usage(tmp_path: Path) -> None:
    # Anthropic-style split: cached reads reported separately from input.
    _write_codex_session(
        tmp_path,
        "def-5678",
        [
            {
                "payload": {
                    "usage": {
                        "input_tokens": 1_000,
                        "cache_read_tokens": 150_000,
                        "cache_write_tokens": 2_000,
                    }
                }
            }
        ],
    )
    ctx, _model = cs._codex_probe("def-5678", root=tmp_path)
    assert ctx == 153_000


def test_codex_probe_model_couples_to_returned_context(tmp_path: Path) -> None:
    # Codex carries the model on a `turn_context` entry preceding the usage.
    # A later model-bearing entry without usage must not overwrite the model
    # that produced the returned context (mismatched-pricing regression).
    _write_codex_session(
        tmp_path,
        "ghi-9012",
        [
            {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
            {"usage": {"input_tokens": 80_000, "output_tokens": 10}},
            {"type": "turn_context", "payload": {"model": "gpt-4o-mini"}},
        ],
    )
    ctx, model = cs._codex_probe("ghi-9012", root=tmp_path)
    assert ctx == 80_000
    assert model == "gpt-5-codex"


def test_codex_probe_missing_session(tmp_path: Path) -> None:
    assert cs._codex_probe("nope", root=tmp_path) == (0, "")


def test_opencode_probe_reads_latest_step_usage(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("m1", "s1", 1, json.dumps({"role": "assistant", "providerID": "openai", "modelID": "gpt-5.5"})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
        (
            "p1",
            "m1",
            "s1",
            2,
            json.dumps(
                {
                    "type": "step-finish",
                    "tokens": {"input": 10_000, "cache": {"read": 140_000, "write": 2_000}},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    assert cs._opencode_probe("s1", db_path=db_path) == (152_000, "openai/gpt-5.5")


def test_cursor_probe_reads_latest_bubble_token_count(tmp_path: Path) -> None:
    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:comp-1:b1",
            json.dumps(
                {
                    "type": 2,
                    "tokenCount": {"inputTokens": 30_000, "cacheReadTokens": 90_000},
                    "modelInfo": {"modelName": "claude-sonnet-4-5"},
                }
            ),
        ),
    )
    # Newer bubble without tokenCount (the common Cursor case) must not mask
    # the older measured one.
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:comp-1:b2", json.dumps({"type": 2, "text": "hi"})),
    )
    # Sibling session must not leak in.
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:comp-other:b9",
            json.dumps({"type": 2, "tokenCount": {"inputTokens": 999_999}}),
        ),
    )
    conn.commit()
    conn.close()

    assert cs._cursor_probe("comp-1", db_path=db_path) == (120_000, "claude-sonnet-4-5")


def test_cursor_probe_no_token_count_returns_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:comp-1:b1", json.dumps({"type": 2, "text": "hi"})),
    )
    conn.commit()
    conn.close()
    assert cs._cursor_probe("comp-1", db_path=db_path) == (0, "")


def _write_copilot_session(root: Path, session_id: str, events: list[dict[str, Any]]) -> None:
    d = root / session_id
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_copilot_probe_uses_latest_compaction_usage(tmp_path: Path) -> None:
    _write_copilot_session(
        tmp_path,
        "cop-1",
        [
            {"type": "assistant.message", "data": {"outputTokens": 50, "model": "gpt-5"}},
            {
                "type": "session.compaction_complete",
                "data": {
                    "compactionTokensUsed": {
                        "inputTokens": 100_000,
                        "cacheReadTokens": 20_000,
                        "cacheWriteTokens": 1_000,
                        "model": "gpt-5",
                    }
                },
            },
        ],
    )
    assert cs._copilot_probe("cop-1", root=tmp_path) == (121_000, "gpt-5")


def test_copilot_probe_shutdown_metrics(tmp_path: Path) -> None:
    _write_copilot_session(
        tmp_path,
        "cop-2",
        [
            {
                "type": "session.shutdown",
                "data": {"modelMetrics": {"gpt-5": {"usage": {"inputTokens": 42_000, "cacheReadTokens": 8_000}}}},
            },
        ],
    )
    assert cs._copilot_probe("cop-2", root=tmp_path) == (50_000, "gpt-5")


def test_copilot_probe_output_only_session_returns_unknown(tmp_path: Path) -> None:
    _write_copilot_session(
        tmp_path,
        "cop-3",
        [{"type": "assistant.message", "data": {"outputTokens": 50}}],
    )
    assert cs._copilot_probe("cop-3", root=tmp_path) == (0, "")
