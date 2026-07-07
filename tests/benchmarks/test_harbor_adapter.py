"""Tests for the Harbor Terminal-Bench adapter (benchmarks/harbor/atelier_agent.py).

The harbor framework is installed as a uv tool, not a project dependency, so
these tests skip unless harbor is importable. To run them locally:

    PYTHONPATH=$HOME/.local/share/uv/tools/harbor/lib/python3.13/site-packages \
        uv run pytest tests/benchmarks/test_harbor_adapter.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# NOT plain "harbor": with the repo root on sys.path that name can resolve to
# the local benchmarks/harbor package; require the real framework module.
pytest.importorskip("harbor.agents.installed.base")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.harbor import atelier_agent  # noqa: E402
from benchmarks.harbor.atelier_agent import AtelierClaudeCodeHarborAgent  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-FAKETESTTOKEN123456"


class _Ctx:
    """Minimal AgentContext stand-in."""

    n_input_tokens = 0
    n_cache_tokens = 0
    n_output_tokens = 0
    cost_usd = 0.0


@pytest.fixture()
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AtelierClaudeCodeHarborAgent:
    # Single-token fallback path: no _1/_2 pool, plain env token.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_1", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_2", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(atelier_agent, "_TOKEN_QUEUE", None)
    monkeypatch.setattr(atelier_agent, "_TOKEN_QUEUE_INIT", False)
    return AtelierClaudeCodeHarborAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")


def _run_and_capture(agent: AtelierClaudeCodeHarborAgent) -> tuple[str, dict[str, str]]:
    captured: dict[str, Any] = {}

    async def fake_exec(environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any) -> None:
        captured["command"] = command
        captured["env"] = env

    agent.exec_as_root = fake_exec  # type: ignore[method-assign]
    # __wrapped__ bypasses the prompt-template decorator (needs no template file).
    asyncio.run(AtelierClaudeCodeHarborAgent.run.__wrapped__(agent, "solve the task", None, _Ctx()))
    return captured["command"], captured["env"]


def test_run_keeps_token_out_of_logged_command(agent: AtelierClaudeCodeHarborAgent) -> None:
    """harbor logs the command verbatim into trial.log; the token must only
    travel via the exec env= dict (dropped by harbor's log formatter)."""
    command, env = _run_and_capture(agent)
    assert FAKE_TOKEN not in command
    assert "sk-ant" not in command
    assert env is not None and env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN


def test_run_uses_harbor_model_and_stages_sessions(agent: AtelierClaudeCodeHarborAgent) -> None:
    command, _ = _run_and_capture(agent)
    # -m anthropic/claude-opus-4-8 -> operational --model claude-opus-4-8
    assert "--model claude-opus-4-8" in command
    # Session JSONLs staged where the ATIF converter expects them, preserving
    # claude's exit code.
    assert "cp -r /root/.claude-bench/projects/. /logs/agent/sessions/projects/" in command
    assert command.rstrip("'").rstrip().endswith("exit $rc")


def _session_event(uuid: str, typ: str, message: dict[str, Any], ts: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "sessionId": "s1",
        "type": typ,
        "timestamp": ts,
        "isSidechain": False,
        "cwd": "/app",
        "version": "2.0.0",
        "message": message,
    }


def test_populate_context_writes_atif_trajectory(agent: AtelierClaudeCodeHarborAgent, tmp_path: Path) -> None:
    """populate_context_post_run must emit agent/trajectory.json (ATIF) with the
    cost patched in from claude-run.json, and still fill the context totals."""
    events = [
        _session_event(
            "u1",
            "user",
            {"role": "user", "content": [{"type": "text", "text": "solve the task"}]},
            "2026-07-01T00:00:00Z",
        ),
        _session_event(
            "a1",
            "assistant",
            {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
            },
            "2026-07-01T00:00:01Z",
        ),
        _session_event(
            "u2",
            "user",
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}],
            },
            "2026-07-01T00:00:02Z",
        ),
        _session_event(
            "a2",
            "assistant",
            {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 20, "output_tokens": 7},
            },
            "2026-07-01T00:00:03Z",
        ),
    ]
    proj = tmp_path / "sessions" / "projects" / "-app"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (tmp_path / "claude-run.json").write_text(
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 1.23,
                "usage": {"input_tokens": 30, "output_tokens": 12, "cache_read_input_tokens": 2},
            }
        )
    )

    ctx = _Ctx()
    agent.populate_context_post_run(ctx)

    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert trajectory["schema_version"].startswith("ATIF")
    assert len(trajectory["steps"]) == 3
    assert trajectory["final_metrics"]["total_cost_usd"] == 1.23
    assert ctx.n_input_tokens == 30
    assert ctx.n_output_tokens == 12
    assert ctx.cost_usd == 1.23


def test_populate_context_without_session_still_fills_totals(
    agent: AtelierClaudeCodeHarborAgent, tmp_path: Path
) -> None:
    """No staged session (e.g. claude crashed early): no trajectory, but the
    claude-run.json totals must still populate the context."""
    (tmp_path / "claude-run.json").write_text(
        json.dumps({"type": "result", "total_cost_usd": 0.5, "usage": {"output_tokens": 3}})
    )
    ctx = _Ctx()
    agent.populate_context_post_run(ctx)
    assert not (tmp_path / "trajectory.json").exists()
    assert ctx.cost_usd == 0.5
    assert ctx.n_output_tokens == 3


def test_supports_atif_and_commit_version(agent: AtelierClaudeCodeHarborAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    assert AtelierClaudeCodeHarborAgent.SUPPORTS_ATIF is True
    monkeypatch.setenv("ATELIER_BENCH_COMMIT", "abc1234")
    assert agent.version() == "abc1234"
