from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.plugin_runtime import (
    aggregate_session_stats,
    apply_session_start_files,
    build_savings_report,
    load_live_savings_summary,
    session_start_bootstrap,
    status_line_choose_message,
    update_session_stats,
    write_plugin_setting,
)
from lemoncrow.core.foundation.paths import session_dir

pytestmark = pytest.mark.slow  # Each test spawns a real Python subprocess (~2s each)

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "claude" / "plugin" / "hooks"


def _run_hook(
    script: str,
    payload: dict[str, Any],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=merged_env,
    )


def test_pre_tool_discipline_does_not_redirect_shell_reads() -> None:
    # The old tool_redirect.py hook rewrote a shell read (cat/rg) into an
    # lc.search PreToolUse nudge. It was removed -- pre_tool_discipline.py's
    # docstring records that the hard shell-read redirect "mis-fired on
    # legitimate searches" -- so a shell read must NOT be blocked or rewritten
    # now.
    result = _run_hook(
        "pre_tool_discipline.py",
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "cat src/app.ts"}},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert "permissionDecision" not in result.stdout


def test_pre_tool_discipline_is_quiet_without_pythonpath() -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rg -n foo ."},
    }
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(HOOKS / "pre_tool_discipline.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_session_telemetry_persists_session_savings(tmp_path: Path) -> None:
    lemoncrow_root = tmp_path / ".lemoncrow"
    _run_hook(
        "session_telemetry.py",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"edits": [{"file_path": "a.ts"}, {"file_path": "b.ts"}]},
        },
        env={"LEMONCROW_ROOT": str(lemoncrow_root)},
    )

    stats = json.loads((session_dir(lemoncrow_root, "claude", "s1") / "stats.json").read_text(encoding="utf-8"))
    assert stats["total_tool_calls"] == 1
    assert stats["edit_tool_calls"] == 1
    assert (session_dir(lemoncrow_root, "claude", "s1") / "events.jsonl").exists()


def test_session_telemetry_tracks_usage_compaction_and_subagents(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"

    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "explore"},
            "usage": {"input_tokens": 5, "output_tokens": 3, "cache_read_input_tokens": 2},
            "now_ms": 1000,
        },
    )
    update_session_stats(root, {"hook_event_name": "PreCompact", "session_id": "s1", "now_ms": 2000})
    update_session_stats(root, {"hook_event_name": "PostCompact", "session_id": "s1", "now_ms": 2750})
    update_session_stats(root, {"hook_event_name": "SubagentStop", "session_id": "s1", "now_ms": 3000})

    stats = json.loads((session_dir(root, "claude", "s1") / "stats.json").read_text(encoding="utf-8"))
    # Only per-turn deltas from payload.usage accumulate; transcript is NOT read here.
    assert stats["usage"]["input_tokens"] == 5
    assert stats["usage"]["output_tokens"] == 3
    assert stats["usage"]["cache_read_tokens"] == 2
    assert stats["compactions"] == 1
    assert stats["compaction_duration_ms"] == 750
    assert stats["subagents_started"] == 1
    assert stats["subagents_completed"] == 1
    assert stats["pending_subagents"] == 0
    assert (session_dir(root, "claude", "s1") / "events.jsonl").exists()


def test_session_telemetry_tracks_spawn_cache_signals(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"

    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Agent",
            "spawn_telemetry": {
                "eligible_for_reuse": True,
                "reuse_observed": False,
                "spawn_latency_ms": 120,
                "cache_capability": "hint_only",
                "host_dropped_fields": ["cache_scope_id", "stable_prefix_hash"],
            },
        },
    )
    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s2",
            "tool_name": "Agent",
            "spawn_telemetry": {
                "eligible_for_reuse": True,
                "reuse_observed": True,
                "spawn_latency_ms": 80,
                "cache_capability": "explicit",
                "host_dropped_fields": ["cache_scope_id"],
            },
        },
    )

    aggregate = aggregate_session_stats(root)

    assert aggregate["spawn_telemetry"]["eligible_for_reuse"] == 2
    assert aggregate["spawn_telemetry"]["reuse_observed"] == 1
    assert aggregate["spawn_telemetry"]["spawn_latency_ms"] == 200
    assert aggregate["spawn_telemetry"]["cache_capability_counts"] == {"hint_only": 1, "explicit": 1}
    assert aggregate["spawn_telemetry"]["host_dropped_fields"] == {"cache_scope_id": 2, "stable_prefix_hash": 1}


def test_context_window_snapshot_overwrites_not_accumulates(tmp_path: Path) -> None:
    """context_window.current_usage is cumulative — must overwrite, not sum.

    The root cause of the historical 17B-token inflation was adding the cumulative
    snapshot on every PostToolUse call (arithmetic series). Calling 5 times with a
    growing snapshot must result in the LAST snapshot value, not the sum of all.
    """
    root = tmp_path / ".lemoncrow"
    for turn, cR in enumerate([1_000, 5_000, 20_000, 80_000, 200_000], start=1):
        update_session_stats(
            root,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "tool_name": "Read",
                "context_window": {
                    "current_usage": {
                        "input_tokens": turn * 10,
                        "output_tokens": turn * 5,
                        "cache_creation_input_tokens": turn * 2,
                        "cache_read_input_tokens": cR,
                    }
                },
            },
        )

    stats = json.loads((session_dir(root, "claude", "s1") / "stats.json").read_text(encoding="utf-8"))
    # Must equal the LAST snapshot values, not the sum over 5 calls.
    assert stats["usage"]["cache_read_tokens"] == 200_000  # last cR snapshot
    assert stats["usage"]["input_tokens"] == 50  # turn=5: 5*10
    assert stats["usage"]["output_tokens"] == 25  # turn=5: 5*5
    assert stats["usage"]["cache_write_tokens"] == 10  # turn=5: 5*2


def test_savings_report_uses_live_events_only(tmp_path: Path) -> None:
    """build_savings_report sums per-event cost_saved_usd values — no synthesis.

    Live events were priced at emit time against the model in use AT THAT TURN.
    A session that never produced a real ``tokens_saved`` measurement has zero
    savings, not a synthesized number.
    """
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "SQL",
            "tool_input": {"queries": [{"sql": "select 1"}]},
        },
    )

    aggregate = aggregate_session_stats(root)
    report = build_savings_report(root)

    assert aggregate["session_count"] == 1
    assert aggregate["total_tool_calls"] == 1
    # No live savings events emitted — no real measurements — so zero savings.
    assert report["calls_avoided"] == 0
    assert report["tokens_saved"] == 0
    assert report["saved_usd"] == 0.0

    # A real per-session savings row: 50k tokens saved, priced at $0.50. The
    # all-sessions aggregate sources realized savings from sessions/*/savings.jsonl
    # (the per-session ledger the statusline / stop hook / web Savings page all
    # use), not the raw live-events log. Fresh root: the zero-call above cached
    # this root's empty window aggregate in-process, and that cache only refreshes
    # against other processes' writes on a throttle -- not sub-second here (a
    # test-timing artifact, not how the CLI reads it, one fresh process per call).
    from datetime import UTC, datetime

    root_b = tmp_path / ".lemoncrow_b"
    sdir = root_b / "sessions" / "s1sess"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "savings.jsonl").write_text(
        json.dumps(
            {
                "tool": "read",
                "tokens": 50_000,
                "calls": 0,
                "cost_saved_usd": 0.5,
                "model": "claude-opus-4-7",
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = build_savings_report(root_b)
    assert report["tokens_saved"] == 50_000
    assert report["saved_usd"] == 0.5
    assert report["cost"]["saved_usd"] == 0.5


def test_session_start_bootstrap_applies_settings_auth_and_always_load(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    write_plugin_setting(root, "alwaysLoadTools", False)
    result = session_start_bootstrap(
        root,
        "/plugin",
        host_settings={},
        mcp_json={"mcpServers": {"lemoncrow": {"alwaysLoad": True}}},
        payload={"session_id": "s1"},
    )

    assert result["host_settings"]["statusLine"]["command"].endswith("/plugin/scripts/statusline.sh")
    assert result["host_settings"]["subagentStatusLine"]["command"].endswith("/plugin/scripts/statusline.sh")
    assert result["host_settings"]["spinnerVerbs"]["mode"] == "replace"
    assert result["host_settings"]["spinnerVerbs"]["verbs"]
    assert result["host_settings"]["lemoncrow"]["attribution"]["source"] == "LemonCrow"
    assert result["host_settings"]["includeCoAuthoredBy"] is False
    assert result["mcp_json"]["mcpServers"]["lemoncrow"]["alwaysLoad"] is False
    assert result["auth"]["isAnonymous"] is True
    assert "LemonCrow budget optimizer" in result["stdout"]["additionalContext"]
    assert (session_dir(root, "claude", "s1") / "stats.json").exists()


def test_spinner_setting_writes_top_level_object() -> None:
    from lemoncrow.core.capabilities.plugin_runtime import apply_spinner_setting

    out = apply_spinner_setting({}, True)
    assert out["spinnerVerbs"]["mode"] == "replace"
    assert out["spinnerVerbs"]["verbs"]
    # No inert namespaced key is written.
    assert "lemoncrow" not in out
    # Disabling removes the top-level key.
    assert "spinnerVerbs" not in apply_spinner_setting({"spinnerVerbs": {"mode": "replace", "verbs": ["x"]}}, False)


def test_attribution_suppresses_coauthor_with_guard() -> None:
    from lemoncrow.core.capabilities.plugin_runtime import apply_attribution_setting

    # Absent key -> we suppress Claude's trailer.
    out = apply_attribution_setting({}, True)
    assert out["includeCoAuthoredBy"] is False
    assert out["lemoncrow"]["attribution"]["enabled"] is True
    # User already set the key -> never override it.
    out_user = apply_attribution_setting({"includeCoAuthoredBy": True}, True)
    assert out_user["includeCoAuthoredBy"] is True
    # Disabling drops bookkeeping and leaves includeCoAuthoredBy untouched.
    out_off = apply_attribution_setting({"includeCoAuthoredBy": False}, False)
    assert out_off["includeCoAuthoredBy"] is False
    assert "lemoncrow" not in out_off


def test_claude_session_start_hook_prints_optimizer_context(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    plugin_root = tmp_path / "plugin"
    config_dir = tmp_path / "claude"
    plugin_root.mkdir()
    (plugin_root / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    result = _run_hook(
        "session_start.py",
        {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"},
        env={
            "LEMONCROW_ROOT": str(root),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "CLAUDE_CONFIG_DIR": str(config_dir),
        },
    )

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "smallest viable plan" in output["additionalContext"]


def test_claude_stop_hook_shows_cache_and_estimated_session_savings(tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / ".lemoncrow"
    stats_dir = session_dir(root, "claude", "s1")
    stats_dir.mkdir(parents=True)
    (stats_dir / "stats.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "total_tool_calls": 4,
                "edit_tool_calls": 0,
                "usage": {
                    "input_tokens": 150_000,
                    "output_tokens": 2_000,
                    "cache_read_tokens": 9_000,
                    "cache_write_tokens": 700,
                },
            }
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "message": {
                    "usage": {
                        "input_tokens": 107_386,
                        "output_tokens": 356,
                        "cache_read_input_tokens": 500,
                        "cache_creation_input_tokens": 10,
                    },
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__mcp-vector-search__codegraph_context",
                        }
                    ],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_hook(
        "stop.py",
        {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "transcript_path": str(transcript),
            "total_cost_usd": 0.1683,
        },
        env={"LEMONCROW_ROOT": str(root)},
    )

    output = json.loads(result.stdout)
    message = output["systemMessage"]
    assert "Session stats:" in message
    # Stats are computed from the transcript (1 turn, 1 tool_use here), one dense
    # line per metric in the trimmed format.
    assert "1 turn · 1 tool call" in message
    assert "tokens: 107.4k in (107.4k new + 10 cW) · 500 cR · 356 out · 108.3k total" in message
    # Savings come from transcript saved blocks — none in this test transcript, so $0.
    assert "savings: $0.00 · 0 tok · 0 calls avoided" in message
    assert "top tools: mcp__mcp-vector-search__codegraph_context" in message


def test_claude_stop_hook_dedupes_usage_and_prices_each_model(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "session.jsonl"
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
            "content": [{"type": "tool_use", "id": "toolu-opus", "name": "Edit", "input": {}}],
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
            "content": [{"type": "tool_use", "id": "toolu-sonnet", "name": "Read", "input": {}}],
        },
    }
    transcript.write_text(
        "\n".join(json.dumps(event) for event in (opus_turn, opus_turn, sonnet_turn, sonnet_turn)) + "\n",
        encoding="utf-8",
    )

    result = _run_hook(
        "stop.py",
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        env={"LEMONCROW_ROOT": str(root)},
    )

    output = json.loads(result.stdout)
    message = output["systemMessage"]
    assert "2 turns · 2 tool calls" in message
    assert "tokens: 6.0k in (3.0k new + 3.0k cW) · 3.0k cR · 3.0k out · 12.0k total" in message
    assert "est. cost: ~$0.08" in message
    assert "top tools: Edit" in message
    assert "Read" in message


def test_apply_session_start_files_mutates_host_settings_and_plugin_mcp(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    config_dir = tmp_path / "config"
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lemoncrow": {"alwaysLoad": False}}}),
        encoding="utf-8",
    )
    write_plugin_setting(root, "alwaysLoadTools", True)

    apply_session_start_files(root, plugin_root, config_dir=config_dir, payload={"session_id": "s2"})

    settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    mcp_json = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"].endswith("/plugin/scripts/statusline.sh")
    assert settings["subagentStatusLine"]["command"].endswith("/plugin/scripts/statusline.sh")
    assert mcp_json["mcpServers"]["lemoncrow"]["alwaysLoad"] is True


def test_session_start_bootstrap_preserves_existing_statusline_command(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    existing = "/custom/path/statusline.sh"
    result = session_start_bootstrap(
        root,
        "/plugin",
        host_settings={
            "statusLine": {"type": "command", "command": existing, "padding": 1},
            "subagentStatusLine": {"type": "command", "command": existing, "padding": 1},
        },
        mcp_json={"mcpServers": {"lemoncrow": {"alwaysLoad": True}}},
    )

    assert result["host_settings"]["statusLine"]["command"] == existing
    assert result["host_settings"]["subagentStatusLine"]["command"] == existing
    assert result["host_settings"]["statusLine"]["padding"] == 1
    assert result["host_settings"]["subagentStatusLine"]["padding"] == 1


def test_savings_report_includes_lifetime_baseline_and_ab_calibration(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    (root / "lifetime_savings.json").write_text(json.dumps({"calls_saved": 8}), encoding="utf-8")
    (root / "baseline_estimate.json").write_text(
        json.dumps({"vanillaSessions": 6, "totalVanillaCostInUsd": 12.0}),
        encoding="utf-8",
    )
    # A/B calibration. Three rows of
    # measured LemonCrow-vs-native read deltas (ratios 0.10/0.12/0.20 → median 0.12).
    (root / "savings_calibration.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "tool": "read",
                        "language": "python",
                        "ratio": 0.10,
                        "token_ratio": 0.15,
                        "chars_saved": 90_000,
                    }
                ),
                json.dumps(
                    {
                        "tool": "read",
                        "language": "python",
                        "ratio": 0.12,
                        "token_ratio": 0.18,
                        "chars_saved": 70_000,
                    }
                ),
                json.dumps(
                    {
                        "tool": "read",
                        "language": "go",
                        "ratio": 0.40,
                        "token_ratio": 0.45,
                        "chars_saved": 30_000,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_savings_report(root)

    assert report["lifetime"]["calls_saved"] == 8
    assert report["baseline"]["available"] is True
    ab = report["ab_calibration"]
    assert ab["samples"] == 3
    read = ab["by_tool"]["read"]
    assert read["n"] == 3
    assert read["median_ratio"] == 0.12  # 3-row median = middle value
    assert read["median_token_ratio"] == 0.18
    # Per-language breakdown so a dashboard can't show one inflated number that
    # hides the generic-outline weakness on languages without an AST builder.
    assert read["by_language"]["python"]["n"] == 2
    assert read["by_language"]["python"]["median_token_saved_pct"] == 83.5
    assert read["by_language"]["go"]["n"] == 1
    assert read["by_language"]["go"]["median_token_saved_pct"] == 55.0


def test_savings_report_omits_ab_calibration_when_no_runs(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    report = build_savings_report(root)
    # No calibration file → empty dict, not missing key, so dashboards can
    # rely on the field's presence.
    assert report["ab_calibration"] == {}


def test_live_savings_summary_counts_cost_only_routing_events(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    (root / "live_savings_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_id": "s1",
                        "lever": "session_compaction",
                        "tokens_saved": 42_000,
                        "calls_saved": 0,
                        "cost_saved_usd": 0.64,
                    }
                ),
                json.dumps(
                    {
                        "session_id": "s1",
                        "lever": "model_routing",
                        "tokens_saved": 0,
                        "calls_saved": 0,
                        "cost_saved_usd": 0.23,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    # load_live_savings_summary still reads live_savings_events.jsonl (the raw
    # per-event log). The all-sessions report, however, sources realized savings
    # from sessions/*/savings.jsonl (c06ffd6d) with routing folded into saved_usd,
    # so mirror the same context + routing savings there for the report asserts.
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    sdir = root / "sessions" / "s1sess"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "savings.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"tool": "read", "tokens": 42_000, "calls": 0, "cost_saved_usd": 0.64, "ts": now}),
                json.dumps({"kind": "routing", "usd": 0.23, "tool": "edit", "model": "claude-sonnet-4-5", "ts": now}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = load_live_savings_summary(root, session_id="s1")
    report = build_savings_report(root)

    assert summary == {
        "calls_saved": 0,
        "tokens_saved": 42_000,
        "saved_usd": 0.87,
        "routing_saved_usd": 0.23,
    }
    # Report sources realized savings from the per-session ledger: read $0.64 +
    # routing $0.23 (folded) = $0.87.
    assert report["cost"]["saved_usd"] == 0.87
    assert report["cost"]["live_saved_usd"] == 0.87
    assert report["cost"]["routing_saved_usd"] == 0.23


def test_statusline_shows_routing_savings(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_dir(root, "claude", "s1").mkdir(parents=True)
    (root / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    (session_dir(root, "claude", "s1") / "stats.json").write_text(
        json.dumps({"session_id": "s1", "savings": {"calls_saved": 1, "tokens_saved": 10_000}}),
        encoding="utf-8",
    )
    (root / "live_savings_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_id": "s1",
                        "lever": "session_compaction",
                        "tokens_saved": 42_000,
                        "cost_saved_usd": 0.64,
                    }
                ),
                json.dumps(
                    {
                        "session_id": "s1",
                        "lever": "model_routing",
                        "tokens_saved": 0,
                        "cost_saved_usd": 0.23,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "integrations" / "claude" / "plugin" / "scripts" / "statusline.sh")],
        input=json.dumps(
            {
                "session_id": "s1",
                "model": {"display_name": "Sonnet"},
                "context_window": {
                    "used_percentage": 42,
                    "current_usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
                "cost": {"total_cost_usd": 1.23, "total_duration_ms": 61_000},
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "LEMONCROW_ROOT": str(root), "LEMONCROW_NO_COLOR": "1"},
    )

    assert "routing: $0.23" in result.stdout
    # Format: "$0.87(42k)" — saved USD with token count in parens.
    # No calls-saved counter (hidden until calibration store feeds equivalent_calls).
    assert "$0.87(42k)" in result.stdout
    assert "calls saved" not in result.stdout
    assert "↓ $0.87" in result.stdout


def test_status_line_priority_and_weighted_rotation() -> None:
    assert status_line_choose_message(update_flag={"fromVersion": "1", "toVersion": "2"})["message_family"] == "update"
    assert (
        status_line_choose_message(auth_present=False, update_flag={"fromVersion": "1", "toVersion": "2"})[
            "message_family"
        ]
        == "login"
    )
    assert status_line_choose_message(auth_present=False)["message_family"] == "login"
    assert status_line_choose_message(subscription_warning=True)["message_family"] == "subscription"

    rotated = status_line_choose_message(
        session_id="s1",
        total_tool_calls=3,
        turn_count=6,
        enabled_families=["savings", "baseline", "tip"],
    )
    assert rotated["message_family"] in {"savings", "baseline", "tip"}
    assert rotated["rotation_skipped"] is False
