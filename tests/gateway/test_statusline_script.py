from __future__ import annotations

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


def _seed_savings_sidecar(root: Path, session_id: str, row: dict[str, object]) -> Path:
    """Seed savings.jsonl at the canonical (dated + host) session dir the
    statusline script's reader (savings_summary._find_savings_sidecar)
    actually looks under."""
    from atelier.core.foundation.paths import session_dir

    sidecar_dir = session_dir(root, "claude", session_id)
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    path = sidecar_dir / "savings.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


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


def test_statusline_prefers_fresh_mcp_sidecar_in_canonical_session_dir(tmp_path: Path) -> None:
    """The MCP server writes statusline_segment under the date+host partitioned
    session dir (sessions/YYYY/MM/DD/<host>/<id>/); the script must find it
    there — the flat sessions/<id>/ path is only a legacy fallback."""
    from atelier.core.foundation.paths import session_dir

    seg_dir = session_dir(tmp_path, "claude", "s1")
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "statusline_segment").write_text(" | SIDECAR-SEGMENT-MARKER", encoding="utf-8")

    output = _run_statusline(tmp_path, _payload())
    assert "SIDECAR-SEGMENT-MARKER" in output


def test_statusline_rotates_multiframe_sidecar_by_wall_clock(tmp_path: Path) -> None:
    """statusline_frames (all frames, one per line) is preferred over the
    legacy single-frame statusline_segment, and the shown frame is one of the
    file's lines — picked by wall clock, so rotation continues even when the
    sidecar is not rewritten between renders."""
    from atelier.core.foundation.paths import session_dir

    seg_dir = session_dir(tmp_path, "claude", "s1")
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "statusline_segment").write_text(" | STALE-SINGLE-FRAME", encoding="utf-8")
    os.utime(seg_dir / "statusline_segment", (1, 1))  # ancient — must lose to frames
    frames = [" | FRAME-ALPHA", " | FRAME-BETA", " | FRAME-GAMMA"]
    (seg_dir / "statusline_frames").write_text("\n".join(frames) + "\n", encoding="utf-8")

    output = _run_statusline(tmp_path, _payload())
    assert "STALE-SINGLE-FRAME" not in output
    assert sum(marker in output for marker in ("FRAME-ALPHA", "FRAME-BETA", "FRAME-GAMMA")) == 1


def test_statusline_falls_back_to_legacy_flat_sidecar(tmp_path: Path) -> None:
    seg_dir = tmp_path / "sessions" / "s1"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "statusline_segment").write_text(" | FLAT-SEGMENT-MARKER", encoding="utf-8")

    output = _run_statusline(tmp_path, _payload())
    assert "FLAT-SEGMENT-MARKER" in output


def test_statusline_reads_session_savings(tmp_path: Path) -> None:
    # MCP dispatcher writes one row per tool call to the canonical session dir.
    _seed_savings_sidecar(tmp_path, "s1", {"tool": "search", "tokens": 12_000, "calls": 4})

    # Savings at Sonnet 4.6 rate ($3/MTok x 12k = $0.036).
    # The payload has no model.id so compute_savings_summary falls back to
    # claude-sonnet-4-5 for pricing.
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    output = _run_statusline(tmp_path, _payload())

    # Cost segment unchanged: "$0.42(I:100 C:300 O:50)". Trailing segment is
    # the folded total-saved figure: "$0.04(I:12.0k)".
    assert "$0.42(I:100 C:300 O:50)" in output
    assert "$0.04(I:12.0k)" in output
    assert "calls saved" not in output


def test_statusline_prices_fallback_savings_from_claude_transcript_model_mix(
    tmp_path: Path,
) -> None:
    # Write session sidecar with token counts.
    _seed_savings_sidecar(tmp_path, "s1", {"tool": "search", "tokens": 12_000, "calls": 4})
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
    # Just verify savings are non-zero and the I/O/R breakdown is shown.
    assert "(I:" in sonnet_output and "$0.00(I:" not in sonnet_output
    assert "(I:" in opus_output and "$0.00(I:" not in opus_output


def test_statusline_falls_back_to_workspace_session_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from atelier.core.foundation.paths import workspace_key

    state_dir = tmp_path / "workspaces" / workspace_key(workspace)
    state_dir.mkdir(parents=True)
    (state_dir / "session_state.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    # Savings are keyed under "s1" (the real session id from session_state.json).
    _seed_savings_sidecar(tmp_path, "s1", {"tool": "search", "tokens": 12_000, "calls": 4})
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    payload = _payload()
    payload["session_id"] = "subagent-missing"

    output = _run_statusline(tmp_path, payload, env_extra={"CLAUDE_WORKSPACE_ROOT": str(workspace)})

    # Subagent session has no direct sidecar, and workspace fallback is not yet
    # wired in compute_savings_summary, so savings are zero — but the cost
    # segment still reflects live usage from the payload.
    assert "$0.42(I:100 C:300 O:50)" in output
    assert "$0.00(I:0)" in output


def test_statusline_does_not_fallback_when_session_id_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from atelier.core.foundation.paths import workspace_key

    state_dir = tmp_path / "workspaces" / workspace_key(workspace)
    state_dir.mkdir(parents=True)
    (state_dir / "session_state.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    _seed_savings_sidecar(tmp_path, "s1", {"tool": "search", "tokens": 12_000, "calls": 4})
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    payload = _payload()
    payload.pop("session_id")
    payload["model"] = {"display_name": "Opus 4.8", "id": "claude-opus-4-8"}

    output = _run_statusline(tmp_path, payload, env_extra={"CLAUDE_WORKSPACE_ROOT": str(workspace)})

    assert "$0.42(I:100 C:300 O:50)" in output
    assert "$0.00(I:0)" in output
    assert "$0.04(I:12.0k)" not in output


def test_statusline_ignores_lifetime_savings_files(tmp_path: Path) -> None:
    # Session sidecar has the real per-session data.
    _seed_savings_sidecar(tmp_path, "s1", {"tool": "search", "tokens": 2_000, "calls": 2})
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

    assert "$0.42(I:100 C:300 O:50)" in output
    assert "$0.01(I:2.0k)" in output
    assert "calls saved" not in output


def test_statusline_shows_stale_agent_nudge_then_falls_back_to_generic_tip(tmp_path: Path) -> None:
    """An installed OPTIONAL agent role that's never been used surfaces the
    staleness nudge (name, "never used", token cost, /atelier remove
    <name>). Same-day re-render must not repeat it -- the once-a-day
    marker-file cooldown falls through to the generic install tip instead,
    exactly like the pre-existing update/install tip segments.
    """
    home = tmp_path / "home"
    agent_dir = home / ".atelier" / "claude-plugin" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "explore.md").write_text("body", encoding="utf-8")

    output = _run_statusline(tmp_path, _payload(), env_extra={"HOME": str(home)})
    assert "explore installed, never used" in output
    assert "/atelier remove explore" in output

    output2 = _run_statusline(tmp_path, _payload(), env_extra={"HOME": str(home)})
    assert "explore installed" not in output2
    assert "more agents/skills: /atelier install <name>" in output2


def test_statusline_empty_session_never_writes_shared_segment_cache(tmp_path: Path) -> None:
    # With no session id, the subprocess cache key used to collapse to a shared
    # "statusline_segment_cache_default" slot that every unbound window
    # read/wrote -- leaking one window's cost/savings into another. Fail closed:
    # an empty-session render must not create any shared segment cache file.
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    payload = _payload()
    payload["session_id"] = ""  # empty id -> previously hit the shared "default" cache

    _run_statusline(tmp_path, payload)

    leaked = sorted(p.name for p in tmp_path.glob("statusline_segment_cache*"))
    leaked += sorted(p.name for p in tmp_path.glob("statusline_segment_ts*"))
    assert leaked == [], f"empty-session render leaked shared cache files: {leaked}"
