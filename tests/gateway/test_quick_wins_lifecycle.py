"""Focused regressions for cross-host context, status, and accounting fixes."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from lemoncrow.core.capabilities.savings_summary import (
    _row_epoch,
    transcript_context_from_path,
    transcript_context_state,
)
from lemoncrow.gateway.adapters.mcp.session_state import mcp_pid_is_live
from lemoncrow.gateway.hosts.context_state import _opencode_probe


def _write_lines(path: Path, *entries: dict) -> None:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


def _usage(count: int) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-5",
            "usage": {
                "input_tokens": count,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def test_core_context_probe_does_not_reuse_precompact_tokens(tmp_path: Path) -> None:
    transcript = tmp_path / "sid.jsonl"
    boundary = {"type": "system", "subtype": "compact_boundary"}
    _write_lines(transcript, _usage(380_000), boundary, {"type": "user", "message": {"content": "summary"}})
    with patch(
        "lemoncrow.core.capabilities.savings_summary.claude_transcript_candidates",
        return_value=[transcript],
    ):
        assert transcript_context_state("sid") == (0, "")
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_usage(28_000)) + "\n")
    assert transcript_context_from_path(transcript) == (28_000, "claude-sonnet-4-5")


def test_bounded_probe_does_not_recover_obsolete_usage_from_long_summary(tmp_path: Path) -> None:
    transcript = tmp_path / "sid.jsonl"
    _write_lines(
        transcript,
        _usage(380_000),
        {"type": "system", "subtype": "compact_boundary"},
        {"type": "user", "message": {"content": "s" * 70_000}},
    )
    assert transcript_context_from_path(transcript) == (0, "")


def test_offset_aware_ledger_timestamps_convert_to_utc() -> None:
    ts = _row_epoch("2026-11-08T00:30:00+02:00")
    assert ts is not None
    assert datetime.fromtimestamp(ts, tz=UTC).isoformat() == "2026-11-07T22:30:00+00:00"
    naive = _row_epoch("2026-11-07T22:30:00")
    assert naive == ts  # legacy naive timestamps are UTC


def test_opencode_v2_includes_cache_write_tokens(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(
            "CREATE TABLE session_v2 (id TEXT);"
            "CREATE TABLE session_message (session_id TEXT, type TEXT, data TEXT, seq INTEGER);"
        )
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?)",
            (
                "s1",
                "assistant",
                json.dumps(
                    {
                        "info": {
                            "role": "assistant",
                            "tokens": {"input": 1000, "cache": {"read": 100_000, "write": 7000}},
                            "modelID": "model",
                        }
                    }
                ),
                1,
            ),
        )
        db.commit()
    finally:
        db.close()
    assert _opencode_probe("s1", db_path=path) == (108_000, "model")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([b"/opt/calculate_mcp"], False),
        ([b"python", b"-m", b"unrelated_module", b"mcp"], False),
        ([b"/opt/lemoncrow", b"rt", b"mcp", b"serve"], True),
        ([b"/opt/lc.real", b"mcp", b"serve"], True),
        ([b"python", b"-m", b"lemoncrow.gateway.cli", b"mcp"], True),
    ],
)
def test_mcp_registration_identifies_real_executable(argv: list[bytes], expected: bool) -> None:
    with (
        patch("lemoncrow.gateway.adapters.mcp.session_state.pid_is_running", return_value=True),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_bytes", return_value=b"\0".join(argv) + b"\0"),
    ):
        assert mcp_pid_is_live(1234) is expected


def test_statusline_never_executes_path_from_transcript(tmp_path: Path) -> None:
    import subprocess

    from tests.gateway.test_statusline_script import SCRIPT

    marker = tmp_path / "unexpected_execution"
    transcript = tmp_path / "sid.jsonl"
    # This payload used to be interpolated into awk system("test -e ...").
    # The dollar/IFS expansion runs even inside double quotes in the old code.
    malicious_path = "/tmp/$(touch${IFS}" + str(marker) + ")"
    _write_lines(
        transcript,
        {
            "type": "user",
            "tool_use_id": "x",
            "message": {
                "content": "Command running in background with ID: a123 Output is being written to: " + malicious_path
            },
        },
    )
    payload = {
        "session_id": "sid",
        "model": {"display_name": "Sonnet", "id": "claude-sonnet-4-5"},
        "transcript_path": str(transcript),
        "context_window": {"used_percentage": 12, "current_usage": {"input_tokens": 100}},
        "cost": {"total_cost_usd": 0.42},
    }
    env = os.environ.copy()
    env.update({"LEMONCROW_ROOT": str(tmp_path / "store"), "LEMONCROW_NO_COLOR": "1"})
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=True,
    )
    assert "Sonnet" in result.stdout
    assert not marker.exists()
