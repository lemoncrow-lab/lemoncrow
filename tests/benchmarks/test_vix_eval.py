from __future__ import annotations

import csv
import importlib
import json
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[2]


def _ensure_benchmarks_package() -> None:
    benchmarks_pkg = types.ModuleType("benchmarks")
    benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
    vix_pkg = types.ModuleType("benchmarks.vix_eval")
    vix_pkg.__path__ = [str(ROOT / "benchmarks" / "vix_eval")]
    sys.modules["benchmarks"] = benchmarks_pkg
    sys.modules["benchmarks.vix_eval"] = vix_pkg


def _load(module_name: str) -> ModuleType:
    _ensure_benchmarks_package()
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


VIX = _load("benchmarks.vix_eval.run")
TASKS = _load("benchmarks.vix_eval.tasks")


def test_write_csv_artifacts_emits_detail_and_summary(tmp_path: Path) -> None:
    results = [
        VIX.ArmResult(
            task="task-1",
            arm="baseline",
            rep=0,
            ok=True,
            cost_usd=1.25,
            duration_ms=1000,
            duration_api_ms=800,
            num_turns=3,
            input_tokens=100,
            cache_read_tokens=10,
            cache_creation_tokens=0,
            output_tokens=25,
            models=["sonnet"],
            is_error=False,
            result_excerpt="ok",
            flow_path="baseline.flow",
        ),
        VIX.ArmResult(
            task="task-1",
            arm="atelier",
            rep=0,
            ok=True,
            cost_usd=0.75,
            duration_ms=700,
            duration_api_ms=500,
            num_turns=2,
            input_tokens=70,
            cache_read_tokens=20,
            cache_creation_tokens=5,
            output_tokens=20,
            models=["sonnet"],
            is_error=False,
            result_excerpt="ok",
            flow_path="atelier.flow",
        ),
        VIX.ArmResult(
            task="task-1",
            arm="vix",
            rep=0,
            ok=True,
            cost_usd=1.0,
            duration_ms=900,
            duration_api_ms=650,
            num_turns=2,
            input_tokens=80,
            cache_read_tokens=15,
            cache_creation_tokens=0,
            output_tokens=22,
            models=["sonnet"],
            is_error=False,
            result_excerpt="ok",
            flow_path="vix.flow",
        ),
    ]

    VIX.write_csv_artifacts(tmp_path, results)

    with (tmp_path / "results.csv").open("r", encoding="utf-8", newline="") as handle:
        detail_rows = list(csv.DictReader(handle))
    with (tmp_path / "summary.csv").open("r", encoding="utf-8", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))

    assert len(detail_rows) == 3
    assert {row["arm"] for row in summary_rows} == {"baseline", "atelier", "vix"}
    atelier_row = next(row for row in summary_rows if row["arm"] == "atelier")
    vix_row = next(row for row in summary_rows if row["arm"] == "vix")
    assert atelier_row["cost_usd"] == "0.75"
    assert atelier_row["cost_savings_vs_baseline_pct"] == "40.0"
    assert atelier_row["duration_savings_vs_baseline_pct"] == "30.0"
    assert atelier_row["input_token_savings_vs_baseline_pct"] == "30.0"
    assert atelier_row["output_token_savings_vs_baseline_pct"] == "20.0"
    assert atelier_row["valid_runs"] == "1"
    assert vix_row["cost_savings_vs_baseline_pct"] == "20.0"


def test_task_prompt_prefers_variant_prompt_when_prompt_md_missing(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    vix_dir = tmp_path / "vix-eval"
    task_dir = vix_dir / "tasks" / "task2_variant"
    task_dir.mkdir(parents=True)
    (task_dir / "prompt_medium.md").write_text("medium prompt", encoding="utf-8")
    (task_dir / "prompt_hard.md").write_text("hard prompt", encoding="utf-8")
    monkeypatch.setenv("VIX_EVAL_DIR", str(vix_dir))

    task = TASKS.Task("task2", "swift", ("empty",), 1, "task2_variant")

    assert task.prompt() == "hard prompt"


def test_main_resume_skips_existing_runs(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    existing = VIX.ArmResult(
        task="task-1",
        arm="baseline",
        rep=0,
        ok=True,
        cost_usd=1.0,
        duration_ms=10,
        duration_api_ms=9,
        num_turns=1,
        input_tokens=11,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        output_tokens=7,
        models=["sonnet"],
        is_error=False,
        result_excerpt="ok",
        flow_path="baseline.flow",
    )
    (run_dir / "results.jsonl").write_text(json.dumps(existing.__dict__) + "\n", encoding="utf-8")

    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")
    monkeypatch.setattr(VIX, "TASKS", [task])
    monkeypatch.setattr(VIX, "BY_ID", {task.id: task})

    calls: list[tuple[str, str, int]] = []

    def fake_run_arm(
        task_obj: Any,
        arm: str,
        rep: int,
        model: str,
        out_dir: Path,
        timeout: int,
        agent_command: str = "claude",
        transport: str = "cli",
        cli_driver: str = "claude",
        api_provider: str = "ollama",
        api_base_url: str | None = None,
        api_key_env: str | None = None,
        agent_env: dict[str, str] | None = None,
        cli_extra_args: list[str] | tuple[str, ...] = (),
    ) -> Any:
        del (
            model,
            out_dir,
            timeout,
            agent_command,
            transport,
            cli_driver,
            api_provider,
            api_base_url,
            api_key_env,
            agent_env,
            cli_extra_args,
        )
        calls.append((task_obj.id, arm, rep))
        return VIX.ArmResult(
            task=task_obj.id,
            arm=arm,
            rep=rep,
            ok=True,
            cost_usd=0.5,
            duration_ms=5,
            duration_api_ms=4,
            num_turns=1,
            input_tokens=6,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            output_tokens=3,
            models=["sonnet"],
            is_error=False,
            result_excerpt="ok",
            flow_path=f"{arm}.flow",
        )

    monkeypatch.setattr(VIX, "run_arm", fake_run_arm)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--tasks",
            "task-1",
            "--arms",
            "baseline",
            "atelier",
            "--reps",
            "1",
            "--out",
            str(run_dir),
            "--resume",
        ],
    )

    assert VIX.main() == 0
    assert calls == [("task-1", "atelier", 0)]


def test_parse_copilot_result_reads_jsonl_metrics() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {"content": [{"text": "Final answer"}], "model": "gpt-5.4"},
                }
            ),
            json.dumps(
                {
                    "type": "session.shutdown",
                    "data": {
                        "modelMetrics": {
                            "gpt-5.4": {
                                "inputTokens": 120,
                                "cachedInputTokens": 80,
                                "cacheCreationInputTokens": 5,
                                "outputTokens": 15,
                            }
                        }
                    },
                }
            ),
        ]
    )

    result = VIX._parse_cli_result(
        stdout,
        Path("copilot.flow"),
        "task-1",
        "atelier",
        0,
        "copilot",
        3210,
    )

    assert result.ok is True
    assert result.duration_ms == 3210
    assert result.input_tokens == 40
    assert result.cache_read_tokens == 80
    assert result.cache_creation_tokens == 5
    assert result.output_tokens == 15
    assert result.models == ["gpt-5.4"]
    assert result.result_excerpt == "Final answer"


def test_parse_codex_result_prefers_token_count_totals() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "model": "gpt-5.4",
                    "content": [{"type": "output_text", "text": "Implemented cache"}],
                    "usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 7},
                }
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 200,
                                "cached_input_tokens": 140,
                                "cache_creation_input_tokens": 4,
                                "output_tokens": 20,
                            }
                        },
                    },
                }
            ),
        ]
    )

    result = VIX._parse_cli_result(
        stdout,
        Path("codex.flow"),
        "task-1",
        "atelier",
        0,
        "codex",
        1111,
    )

    assert result.ok is True
    assert result.duration_ms == 1111
    assert result.input_tokens == 60
    assert result.cache_read_tokens == 140
    assert result.cache_creation_tokens == 4
    assert result.output_tokens == 20
    assert result.models == ["gpt-5.4"]
    assert result.result_excerpt == "Implemented cache"


def test_parse_codex_result_reads_item_completed_stream() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "abc"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Done cleanly"}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 9},
                }
            ),
        ]
    )

    result = VIX._parse_cli_result(
        stdout,
        Path("codex-new.flow"),
        "task-1",
        "atelier",
        0,
        "codex",
        2000,
    )

    assert result.ok is True
    assert result.input_tokens == 100
    assert result.cache_read_tokens == 20
    assert result.output_tokens == 9
    assert result.result_excerpt == "Done cleanly"


def test_parse_opencode_result_reads_normalized_events() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "_type": "message",
                    "data": {
                        "role": "assistant",
                        "text": "Patched the cache implementation",
                        "providerID": "openrouter",
                        "modelID": "claude-sonnet",
                    },
                }
            ),
            json.dumps(
                {
                    "_type": "part",
                    "data": {
                        "type": "step-finish",
                        "tokens": {
                            "input": 90,
                            "output": 12,
                            "cache": {"read": 30, "write": 2},
                        },
                    },
                }
            ),
        ]
    )

    result = VIX._parse_cli_result(
        stdout,
        Path("opencode.flow"),
        "task-1",
        "atelier",
        0,
        "opencode",
        2222,
    )

    assert result.ok is True
    assert result.duration_ms == 2222
    assert result.input_tokens == 60
    assert result.cache_read_tokens == 30
    assert result.cache_creation_tokens == 2
    assert result.output_tokens == 12
    assert result.models == ["openrouter/claude-sonnet"]
    assert result.result_excerpt == "Patched the cache implementation"


def test_run_api_arm_uses_openai_compatible_endpoint(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "model": "llama3.2",
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
                }
            ).encode("utf-8")

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(VIX.urllib.request, "urlopen", fake_urlopen)

    result = VIX.run_api_arm(
        task,
        "vix",
        0,
        "llama3.2",
        workspace,
        30,
        api_provider="ollama",
        api_base_url=None,
        api_key_env=None,
    )

    assert result.ok is True
    assert result.models == ["llama3.2"]
    assert result.input_tokens == 12
    assert result.output_tokens == 5
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["payload"]["model"] == "llama3.2"
    assert "Authorization" not in captured["headers"]


def test_validate_result_excerpt_rejects_placeholder_response() -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "I'm ready to help! What would you like to work on?",
    )

    assert valid is False
    assert reason == "generic placeholder response"


def test_validate_result_excerpt_rejects_off_topic_research_response() -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "I need to research how CLI coding agents detect the host IDE/terminal environment. "
        "I'll start by searching the web for Claude Code, Gemini CLI, Cody, and Aider.",
    )

    assert valid is False
    assert "off-topic" in reason


def test_validate_result_excerpt_rejects_harness_error() -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "harness error: Command '['opencode', 'run', '...']' timed out after 60 seconds",
    )

    assert valid is False
    assert reason == "harness/runtime error"


def test_validate_result_excerpt_rejects_zero_overlap_response(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    vix_dir = tmp_path / "vix-eval"
    task_dir = vix_dir / "tasks" / "task1"
    task_dir.mkdir(parents=True)
    (task_dir / "prompt.md").write_text("Build a Swift LRU cache", encoding="utf-8")
    monkeypatch.setenv("VIX_EVAL_DIR", str(vix_dir))
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "Remember stable user preferences and summarize them into ~/.claude/skills/ for reuse.",
    )

    assert valid is False
    assert reason == "no task keyword overlap"


def test_validate_result_excerpt_accepts_task_relevant_summary() -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "Implemented the Swift LRU cache with disk-backed index persistence, atomic writes, "
        "and debounced access-date updates for get and promote.",
    )

    assert valid is True
    assert reason == ""


def test_validate_result_excerpt_rejects_unnecessary_clarification() -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "The workspace contains only the `CLAUDE.md` file. Could you tell me more about what task1 "
        "should do, or should I scaffold something new?",
    )

    assert valid is False
    assert "clarification" in reason or "workspace confusion" in reason


def test_validate_result_excerpt_rejects_generic_capability_intro() -> None:
    task = TASKS.Task("task-1", "swift", ("empty",), 1, "task1")

    valid, reason = VIX._validate_result_excerpt(
        task,
        "Hello! I can help with many tasks:\n\n"
        "- Code development: write and debug code\n"
        "- Code review: inspect PRs and bugs\n"
        "- Research: investigate topics and summarize findings\n"
        "- Project management: plan and track work\n",
    )

    assert valid is False
    assert "off-task capability/list response" in reason


def test_parse_agent_env_supports_empty_values() -> None:
    parsed = VIX._parse_agent_env(
        [
            "ANTHROPIC_BASE_URL=https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN=secret",
            "ANTHROPIC_API_KEY=",
        ]
    )

    assert parsed == {
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": "secret",
        "ANTHROPIC_API_KEY": "",
    }


def test_parse_agent_env_from_host_reads_existing_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    parsed = VIX._parse_agent_env_from_host(["ANTHROPIC_AUTH_TOKEN=OPENROUTER_API_KEY"])

    assert parsed == {"ANTHROPIC_AUTH_TOKEN": "secret"}


def test_parse_agent_env_from_host_reads_repo_env_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("export OPENROUTER_API_KEY=secret-from-file\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(VIX, "REPO_ROOT", tmp_path)

    parsed = VIX._parse_agent_env_from_host(["ANTHROPIC_AUTH_TOKEN=OPENROUTER_API_KEY"])

    assert parsed == {"ANTHROPIC_AUTH_TOKEN": "secret-from-file"}
