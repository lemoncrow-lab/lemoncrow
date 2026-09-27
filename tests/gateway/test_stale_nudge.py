"""Tests for the Phase 5 staleness nudge: usage log, staleness check, CLI.

Covers the usage-log write path (`plugin_runtime.record_optional_use` /
`last_optional_use_ms`), the `update_session_stats` hook integration that
records optional-agent/skill use while never recording the default `code`
role or `lc` skill, the `stale_optional_items` staleness classifier
(installed+optional+stale vs. recently-used vs. not-installed vs. default),
and the `lc stale-nudge` CLI surface.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from lemoncrow.core.capabilities.plugin_runtime import (
    last_optional_use_ms,
    record_optional_use,
    update_session_stats,
)
from lemoncrow.gateway.cli.app import cli
from lemoncrow.gateway.cli.commands import agents_skills as m

REPO_ROOT = Path(__file__).resolve().parents[2]
_DAY_MS = 86_400_000


# --------------------------------------------------------------------------- #
# Usage-log write path
# --------------------------------------------------------------------------- #


def test_record_and_read_optional_use_round_trips(tmp_path: Path) -> None:
    assert last_optional_use_ms(tmp_path, "agent", "explore") is None
    record_optional_use(tmp_path, "agent", "explore", 1_000)
    record_optional_use(tmp_path, "agent", "explore", 5_000)  # later use -> max wins
    record_optional_use(tmp_path, "skill", "recall", 2_000)
    assert last_optional_use_ms(tmp_path, "agent", "explore") == 5_000
    assert last_optional_use_ms(tmp_path, "skill", "recall") == 2_000
    assert last_optional_use_ms(tmp_path, "agent", "plan") is None


def test_update_session_stats_records_optional_agent_dispatch(tmp_path: Path) -> None:
    update_session_stats(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "explore"},
            "now_ms": 1_234,
        },
    )
    assert last_optional_use_ms(tmp_path, "agent", "explore") == 1_234


def test_update_session_stats_never_records_the_default_code_agent(tmp_path: Path) -> None:
    update_session_stats(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "code"},
            "now_ms": 1_234,
        },
    )
    assert not (tmp_path / "optional_usage.jsonl").exists()


def test_update_session_stats_records_optional_skill_invocation(tmp_path: Path) -> None:
    update_session_stats(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Skill",
            "tool_input": {"skill": "recall"},
            "now_ms": 5_555,
        },
    )
    assert last_optional_use_ms(tmp_path, "skill", "recall") == 5_555


def test_update_session_stats_never_records_the_default_lemoncrow_skill(tmp_path: Path) -> None:
    update_session_stats(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Skill",
            "tool_input": {"skill": "lemoncrow"},
            "now_ms": 5_555,
        },
    )
    assert not (tmp_path / "optional_usage.jsonl").exists()


def test_update_session_stats_ignores_unrelated_tool_calls(tmp_path: Path) -> None:
    update_session_stats(
        tmp_path,
        {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash", "now_ms": 10},
    )
    assert not (tmp_path / "optional_usage.jsonl").exists()


# --------------------------------------------------------------------------- #
# stale_optional_items classifier
# --------------------------------------------------------------------------- #


def _install_agent(ws: Path, role_id: str) -> None:
    (ws / ".claude").mkdir(parents=True, exist_ok=True)
    result = CliRunner().invoke(
        cli, ["agent", "install", role_id, "--host", "claude", "--workspace", str(ws), "--yes"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output


def _install_skill(ws: Path, name: str) -> None:
    (ws / ".claude").mkdir(parents=True, exist_ok=True)
    result = CliRunner().invoke(
        cli, ["skill", "install", name, "--host", "claude", "--workspace", str(ws), "--yes"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output


def test_stale_optional_items_flags_installed_never_used_agent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _install_agent(ws, "explore")
    items = m.stale_optional_items("claude", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS)
    by_name = {(i["kind"], i["name"]): i for i in items}
    assert ("agent", "explore") in by_name
    assert by_name[("agent", "explore")]["days_unused"] is None
    assert by_name[("agent", "explore")]["token_cost"] > 0


def test_stale_optional_items_flags_installed_stale_agent_with_day_count(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _install_agent(ws, "explore")
    record_optional_use(tmp_path, "agent", "explore", at_ms=0)
    items = m.stale_optional_items("claude", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS)
    by_name = {(i["kind"], i["name"]): i for i in items}
    assert by_name[("agent", "explore")]["days_unused"] == 10


def test_stale_optional_items_excludes_recently_used_agent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _install_agent(ws, "explore")
    record_optional_use(tmp_path, "agent", "explore", at_ms=9 * _DAY_MS)  # 1 day ago at now=10d
    items = m.stale_optional_items(
        "claude", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS, threshold_days=7
    )
    assert not any(i["kind"] == "agent" and i["name"] == "explore" for i in items)


def test_stale_optional_items_excludes_uninstalled_agent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    # explore was never installed for this workspace.
    items = m.stale_optional_items("claude", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS)
    assert not any(i["kind"] == "agent" and i["name"] == "explore" for i in items)


def test_stale_optional_items_never_flags_default_code_or_lemoncrow_skill(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _install_agent(ws, "explore")  # also brings the default `code` role along
    _install_skill(ws, "recall")  # also ships the default `lc` skill
    items = m.stale_optional_items("claude", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS)
    names = {(i["kind"], i["name"]) for i in items}
    assert ("agent", "code") not in names
    assert ("skill", "lemoncrow") not in names


def test_stale_optional_items_covers_installed_skills_too(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _install_skill(ws, "recall")
    items = m.stale_optional_items("claude", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS)
    by_name = {(i["kind"], i["name"]): i for i in items}
    assert ("skill", "recall") in by_name
    assert by_name[("skill", "recall")]["token_cost"] > 0


def test_stale_optional_items_skips_skills_for_opencode(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".opencode").mkdir(parents=True)
    items = m.stale_optional_items("opencode", ws, root=tmp_path, repo_root=REPO_ROOT, now_ms=10 * _DAY_MS)
    assert not any(i["kind"] == "skill" for i in items)


def test_stale_nudge_days_env_var_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_STALE_NUDGE_DAYS", "1")
    assert m._stale_nudge_days() == 1.0
    monkeypatch.delenv("LEMONCROW_STALE_NUDGE_DAYS")
    assert m._stale_nudge_days() == m.DEFAULT_STALE_NUDGE_DAYS


# --------------------------------------------------------------------------- #
# format_stale_nudge wording
# --------------------------------------------------------------------------- #


def test_format_stale_nudge_never_used_wording() -> None:
    line = m.format_stale_nudge({"kind": "agent", "name": "explore", "days_unused": None, "token_cost": 42})
    assert line == "explore installed, never used — remove: /lemoncrow remove explore (saves ~42 tok/turn)"


def test_format_stale_nudge_days_unused_wording() -> None:
    line = m.format_stale_nudge({"kind": "skill", "name": "recall", "days_unused": 9, "token_cost": 7})
    assert line == "recall installed, unused 9d — remove: /lemoncrow remove recall (saves ~7 tok/turn)"


# --------------------------------------------------------------------------- #
# CLI: lc stale-nudge
# --------------------------------------------------------------------------- #


def test_stale_nudge_cli_prints_pipe_delimited_line_for_stale_item(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _install_agent(ws, "explore")
    with patch.object(m, "_default_lemoncrow_root", return_value=tmp_path):
        with patch.object(m, "_repo_root", return_value=REPO_ROOT):
            result = CliRunner().invoke(
                cli, ["stale-nudge", "--host", "claude", "--workspace", str(ws)], catch_exceptions=False
            )
    assert result.exit_code == 0, result.output
    line = result.output.strip()
    kind, name, days, cost = line.split("|")
    assert (kind, name, days) == ("agent", "explore", "")
    assert int(cost) > 0


def test_stale_nudge_cli_silent_when_nothing_stale(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    with patch.object(m, "_default_lemoncrow_root", return_value=tmp_path):
        with patch.object(m, "_repo_root", return_value=REPO_ROOT):
            result = CliRunner().invoke(
                cli, ["stale-nudge", "--host", "claude", "--workspace", str(ws)], catch_exceptions=False
            )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ""
