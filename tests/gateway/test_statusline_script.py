from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "integrations" / "claude" / "plugin" / "scripts" / "statusline.sh"


def _run_statusline(root: Path, payload: dict[str, object]) -> str:
    env = os.environ.copy()
    env.update({"ATELIER_STORE_ROOT": str(root), "ATELIER_NO_COLOR": "1"})
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


def test_statusline_shows_free_plan_warning(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    (tmp_path / "free_plan.json").write_text(json.dumps({"remaining": 1, "limit": 10}), encoding="utf-8")

    output = _run_statusline(tmp_path, _payload())

    assert "plan 90%" in output


def test_statusline_reads_session_savings(tmp_path: Path) -> None:
    stats_dir = tmp_path / "session_stats"
    stats_dir.mkdir()
    (stats_dir / "s1.json").write_text(
        json.dumps({"savings": {"calls_saved": 4, "tokens_saved": 12_000}}),
        encoding="utf-8",
    )

    output = _run_statusline(tmp_path, _payload())

    assert "saved $0.036" in output
    assert "ctx 12k / 4c" in output


def test_statusline_ignores_lifetime_savings_files(tmp_path: Path) -> None:
    stats_dir = tmp_path / "session_stats"
    stats_dir.mkdir()
    (stats_dir / "s1.json").write_text(
        json.dumps({"savings": {"calls_saved": 2, "tokens_saved": 2_000}}),
        encoding="utf-8",
    )
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

    output = _run_statusline(tmp_path, _payload())

    assert "saved $0.006" in output
    assert "ctx 2k / 2c" in output
