"""Tests for the benchmark CLI subcommand workflow."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.cli import cli
from atelier.gateway.cli.commands import benchmark as benchmark_cmds

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_legacy_top_level_commands_are_removed(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"

    assert runner.invoke(cli, ["--root", str(root), "benchmark-core", "--json"]).exit_code != 0
    assert runner.invoke(cli, ["--root", str(root), "benchmark", "--prompt", "Fix PDP", "--json"]).exit_code != 0


def test_help_command_shows_root_command_help(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"

    root_help = runner.invoke(cli, ["--root", str(root), "help"])
    assert root_help.exit_code == 0, root_help.output
    assert "Commands:" in root_help.output
    assert "benchmark" in root_help.output


def test_benchmark_terminalbench_defaults_to_all_tasks_and_modes(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "terminalbench"])

    assert result.exit_code == 0, result.output
    trial_calls = [cmd for cmd, label, _env in calls if label == "TerminalBench trial"]
    summary_calls = [cmd for cmd, label, _env in calls if label == "TerminalBench summary"]
    assert len(trial_calls) == 20
    assert len(summary_calls) == 1
    assert {cmd[cmd.index("--mode") + 1] for cmd in trial_calls} == {"on", "off"}
    assert {cmd[cmd.index("--task") + 1] for cmd in trial_calls} == {
        "hello-world",
        "fix-pandas-version",
        "incompatible-python-fasttext",
        "csv-to-parquet",
        "fibonacci-server",
        "simple-web-scraper",
        "fix-git",
        "swe-bench-fsspec",
        "swe-bench-langcodes",
        "grid-pattern-transform",
    }


def test_benchmark_swe_defaults_to_real_eval(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    captured: dict[str, object] = {}

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)

    def fake_run_swe_eval(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(benchmark_cmds, "_run_swe_eval", fake_run_swe_eval)

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe"])

    assert result.exit_code == 0, result.output
    assert captured["subset"] == "lite"
    assert captured["split"] == "dev"
    assert captured["slice_expr"] == "0:5"
    assert captured["workers"] == 1
    assert captured["proxy_upstream"] == "http://localhost:11434/v1"


def test_benchmark_vix_wraps_runner(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label, env = calls[0]
    assert label == "VIX benchmark"
    assert cmd[:3] == ["python", "-m", "benchmarks.vix_eval.run"]
    assert "--tasks" in cmd and cmd[cmd.index("--tasks") + 1] == "all"
    assert "--arms" in cmd
    assert cmd[cmd.index("--cli-driver") + 1] == "claude"
    assert cmd[cmd.index("--jobs") + 1] == "1"
    assert cmd[cmd.index("--parallel-scope") + 1] == "task"
    assert env == {"VIX_EVAL_DIR": str(vix_eval_dir.resolve())}


def test_benchmark_vix_accepts_vix_arm_and_api_options(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--arm",
            "baseline",
            "--arm",
            "atelier",
            "--arm",
            "vix",
            "--model",
            "llama3.2",
            "--transport",
            "api",
            "--api-provider",
            "ollama",
            "--launch-ollama",
            "--bridge-wait",
            "0",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "VIX benchmark"
    assert cmd[cmd.index("--arms") + 1 : cmd.index("--reps")] == ["baseline", "atelier", "vix"]
    assert cmd[cmd.index("--model") + 1] == "llama3.2"
    assert cmd[cmd.index("--transport") + 1] == "api"
    assert cmd[cmd.index("--api-provider") + 1] == "ollama"
    assert "--launch-ollama" in cmd
    assert env == {"VIX_EVAL_DIR": str(vix_eval_dir.resolve())}


def test_benchmark_vix_judge_defaults_to_runner_transport(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--transport",
            "cli",
            "--model",
            "claude-sonnet-4-6",
            "--judge",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, _label, _env = calls[0]
    assert "--judge" in cmd
    assert "--judge-provider" not in cmd
    assert "--judge-model" not in cmd
    assert "--judge-transport" not in cmd
    assert cmd[cmd.index("--transport") + 1] == "cli"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_benchmark_vix_openrouter_claude_preset_passes_agent_env(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--transport",
            "cli",
            "--model",
            "openrouter/owl-alpha",
            "--openrouter-claude",
            "--openrouter-key-env",
            "OPENROUTER_API_KEY",
            "--agent-env",
            "EXTRA_FLAG=1",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "VIX benchmark"
    assert "--agent-env" in cmd
    assert "EXTRA_FLAG=1" in cmd
    assert "ANTHROPIC_BASE_URL=https://openrouter.ai/api" in cmd
    assert "ANTHROPIC_API_KEY=" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=OPENROUTER_API_KEY" in cmd
    assert env == {"VIX_EVAL_DIR": str(vix_eval_dir.resolve())}


def test_benchmark_vix_generic_claude_provider_flags_pass_through(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--transport",
            "cli",
            "--model",
            "provider/model-x",
            "--claude-base-url",
            "https://provider.example/api",
            "--claude-auth-token-env",
            "PROVIDER_AUTH",
            "--claude-api-key-env",
            "PROVIDER_API_KEY",
            "--clear-claude-api-key",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "VIX benchmark"
    assert "ANTHROPIC_BASE_URL=https://provider.example/api" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=PROVIDER_AUTH" in cmd
    assert "ANTHROPIC_API_KEY=PROVIDER_API_KEY" in cmd
    assert env == {"VIX_EVAL_DIR": str(vix_eval_dir.resolve())}


def test_benchmark_vix_forwards_cli_driver_and_jobs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--cli-driver",
            "codex",
            "--jobs",
            "3",
            "--parallel-scope",
            "arm",
            "--cli-extra-arg",
            "-c",
            "--cli-extra-arg",
            'model_reasoning_effort="medium"',
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "VIX benchmark"
    assert cmd[cmd.index("--cli-driver") + 1] == "codex"
    assert cmd[cmd.index("--jobs") + 1] == "3"
    assert cmd[cmd.index("--parallel-scope") + 1] == "arm"
    assert "--cli-extra-arg=-c" in cmd
    assert '--cli-extra-arg=model_reasoning_effort="medium"' in cmd


def test_benchmark_vix_named_aws_claude_preset_passes_env(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--transport",
            "cli",
            "--cli-driver",
            "claude",
            "--claude-provider-preset",
            "aws-claude",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "VIX benchmark"
    assert "CLAUDE_CODE_USE_BEDROCK=1" in cmd
    assert "AWS_REGION=AWS_REGION" in cmd


def test_benchmark_vix_rejects_claude_flags_for_non_claude_driver(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    vix_eval_dir = tmp_path / "vix-eval"
    vix_eval_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_ensure_vix_eval_dir", lambda repo_root, path: vix_eval_dir)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "vix",
            "--transport",
            "cli",
            "--cli-driver",
            "copilot",
            "--openrouter-claude",
            "--vix-eval-dir",
            str(vix_eval_dir),
        ],
    )

    assert result.exit_code != 0
    assert "--openrouter-claude only applies to --transport cli --cli-driver claude." in result.output


def test_benchmark_mcp_defaults_jobs_to_auto(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_resolve_mcp_jobs", lambda jobs, repo_root: 6)
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "mcp"])

    assert result.exit_code == 0, result.output
    cmd, _label, _env = calls[0]
    assert cmd[cmd.index("--jobs") + 1] == "6"


def test_benchmark_mcp_passes_parallel_jobs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "mcp", "--jobs", "3"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label, _env = calls[0]
    assert label == "MCP benchmark"
    assert "--jobs" in cmd
    assert cmd[cmd.index("--jobs") + 1] == "3"


def test_benchmark_auto_jobs_uses_full_cpu_up_to_cap(monkeypatch) -> None:
    monkeypatch.setattr(benchmark_cmds, "cpu_count", lambda: 16)

    assert benchmark_cmds._auto_jobs(20, hard_cap=32) == 16

    monkeypatch.setattr(benchmark_cmds, "cpu_count", lambda: 64)
    assert benchmark_cmds._auto_jobs(40, hard_cap=32) == 32
    assert benchmark_cmds._resolve_provider_jobs(0, ["a"] * 40) == 32


def test_benchmark_providers_passes_parallel_jobs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_workspace_dir",
        lambda suite, repo_root, run_id: tmp_path / "workspace",
    )
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "providers", "--jobs", "4"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label, _env = calls[0]
    assert label == "provider benchmark"
    assert "--jobs" in cmd
    assert cmd[cmd.index("--jobs") + 1] == "4"


def test_benchmark_providers_defaults_to_auto_and_cache_root(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_workspace_dir",
        lambda suite, repo_root, run_id: tmp_path / "workspace",
    )
    monkeypatch.setattr(benchmark_cmds, "_resolve_provider_jobs", lambda jobs, providers: 3)
    monkeypatch.setattr(
        benchmark_cmds,
        "_cache_dir",
        lambda suite, repo_root: tmp_path / "cache",
    )
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "providers"])

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "provider benchmark"
    assert cmd[cmd.index("--jobs") + 1] == "3"
    assert cmd[cmd.index("--cache-root") + 1] == str((tmp_path / "cache").resolve())
