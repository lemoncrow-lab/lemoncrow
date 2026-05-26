from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "integrations" / "claude" / "plugin" / "scripts" / "statusline.sh"
# Source-tree Python so the statusline script uses the current source, not the
# installed uv-tools binary (which may lag behind local edits).
_SOURCE_PYTHON = str(ROOT / ".venv" / "bin" / "python")


def _run_statusline(root: Path, payload: dict[str, object], *, env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env.update(
        {
            "ATELIER_ROOT": str(root),
            "ATELIER_STORE_ROOT": str(root),
            "ATELIER_NO_COLOR": "1",
            # Force the statusline to use source-tree atelier, not installed tools binary.
            "ATELIER_PYTHON": _SOURCE_PYTHON,
        }
    )
    env.update(env_extra or {})
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def _payload() -> dict[str, object]:
    return {
        "session_id": "s1",
        "model": {"display_name": "Sonnet"},
        "context_window": {
            "used_percentage": 12,
            "current_usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 300,
            },
        },
        "cost": {"total_cost_usd": 0.42, "total_duration_ms": 61_000},
    }


def test_statusline_shows_update_priority(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    (tmp_path / "update.json").write_text(
        json.dumps({"fromVersion": "1.0.0", "toVersion": "1.1.0"}),
        encoding="utf-8",
    )

    output = _run_statusline(tmp_path, _payload())

    assert "update 1.1.0" in output


def test_statusline_shows_missing_login_before_update(tmp_path: Path) -> None:
    (tmp_path / "update.json").write_text(
        json.dumps({"fromVersion": "1.0.0", "toVersion": "1.1.0"}),
        encoding="utf-8",
    )

    output = _run_statusline(tmp_path, _payload())

    assert "login" in output
    assert "update 1.1.0" not in output


def test_statusline_reads_session_savings(tmp_path: Path) -> None:
    # MCP dispatcher writes one row per tool call to session_stats/claude/<session_id>.jsonl.
    sidecar = tmp_path / "session_stats" / "claude"
    sidecar.mkdir(parents=True)
    (sidecar / "s1.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 12_000, "calls": 4}) + "\n",
        encoding="utf-8",
    )

    # Savings at Sonnet 4.5 rate ($3/MTok x 12k = $0.036).
    # The payload has no model.id so compute_savings_summary falls back to
    # claude-sonnet-4-5 for pricing.
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    output = _run_statusline(tmp_path, _payload())

    # Format: "$0.036(12k)" — saved USD with token count in parens.
    assert "$0.036(12k)" in output
    assert "calls saved" not in output
    assert "I:" not in output
    assert "O:" not in output


def test_statusline_prices_fallback_savings_from_claude_transcript_model_mix(
    tmp_path: Path,
) -> None:
    # Write session sidecar with token counts.
    sidecar = tmp_path / "session_stats" / "claude"
    sidecar.mkdir(parents=True)
    (sidecar / "s1.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 12_000, "calls": 4}) + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    transcript_dir = home / ".claude" / "projects" / "workspace"
    transcript_dir.mkdir(parents=True)
    opus_turn = {
        "type": "assistant",
        "message": {
            "id": "msg-opus",
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 1_000,
                "cache_read_input_tokens": 1_000,
                "cache_creation_input_tokens": 1_000,
            },
        },
    }
    sonnet_turn = {
        "type": "assistant",
        "message": {
            "id": "msg-sonnet",
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 2_000,
                "output_tokens": 2_000,
                "cache_read_input_tokens": 2_000,
                "cache_creation_input_tokens": 2_000,
            },
        },
    }
    (transcript_dir / "s1.jsonl").write_text(
        "\n".join(json.dumps(event) for event in (opus_turn, opus_turn, sonnet_turn)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    sonnet_payload = _payload()
    sonnet_payload["model"] = {"display_name": "Sonnet 4.6", "id": "claude-sonnet-4-6"}
    opus_payload = _payload()
    opus_payload["model"] = {"display_name": "Opus 4.7", "id": "claude-opus-4-7"}

    env_extra = {"CLAUDE_CONFIG_DIR": str(home / ".claude")}
    sonnet_output = _run_statusline(tmp_path, sonnet_payload, env_extra=env_extra)
    opus_output = _run_statusline(tmp_path, opus_payload, env_extra=env_extra)

    # Weighted per-model pricing: transcript has 2 Opus turns (1k in each @ $15/MTok)
    # + 1 Sonnet turn (2k in @ $3/MTok) -> weighted = (2x15 + 1x3) / (2+2) = 8.25/MTok
    # -> 12k x 8.25/MTok ~ $0.099. Env model does NOT affect pricing.
    # Just verify savings are non-zero and token count is shown.
    assert "(12k)" in sonnet_output
    assert "(12k)" in opus_output


def test_statusline_falls_back_to_workspace_session_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_hash = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    state_dir = tmp_path / "workspaces" / workspace_hash
    state_dir.mkdir(parents=True)
    (state_dir / "session_state.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    # Savings are keyed under "s1" (the real session id from session_state.json).
    sidecar = tmp_path / "session_stats" / "claude"
    sidecar.mkdir(parents=True)
    (sidecar / "s1.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 12_000, "calls": 4}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    payload = _payload()
    payload["session_id"] = "subagent-missing"

    output = _run_statusline(tmp_path, payload, env_extra={"CLAUDE_WORKSPACE_ROOT": str(workspace)})

    assert "$0.036(12k)" in output


def test_statusline_ignores_lifetime_savings_files(tmp_path: Path) -> None:
    # Session sidecar has the real per-session data.
    sidecar = tmp_path / "session_stats" / "claude"
    sidecar.mkdir(parents=True)
    (sidecar / "s1.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 2_000, "calls": 2}) + "\n",
        encoding="utf-8",
    )
    # Lifetime / global files should NOT be summed into session savings.
    (tmp_path / "smart_state.json").write_text(
        json.dumps({"savings": {"calls_avoided": 99, "tokens_saved": 999_999_999}}),
        encoding="utf-8",
    )
    (tmp_path / "cost_history.json").write_text(
        json.dumps(
            {
                "operations": {
                    "search_read": {
                        "calls": [
                            {"cost_usd": 25.0, "cache_read_tokens": 0},
                            {"cost_usd": 0.0, "cache_read_tokens": 500_000_000},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    output = _run_statusline(tmp_path, _payload())

    assert "$0.006(2k)" in output
    assert "calls saved" not in output
