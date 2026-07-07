"""Tests for the benchmark CLI subcommand workflow."""

from __future__ import annotations

import json
import shutil
import subprocess
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


def test_benchmark_gate_command_reads_gate_and_optionally_fails(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    run_dir = tmp_path / "terminalbench"
    run_dir.mkdir()
    (run_dir / "benchmark-gate.json").write_text(
        json.dumps({"suite": "terminalbench", "passed": False, "reasons": ["candidate cost was higher"]}),
        encoding="utf-8",
    )

    ok = runner.invoke(cli, ["--root", str(root), "benchmark", "gate", "--run-dir", str(run_dir), "--json"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["suite"] == "terminalbench"

    failed = runner.invoke(
        cli,
        ["--root", str(root), "benchmark", "gate", "--run-dir", str(run_dir), "--require-pass"],
    )
    assert failed.exit_code != 0
    assert "candidate cost was higher" in failed.output


def test_benchmark_harbor_resume_uses_benchmarks_uv_project(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    resume_dir = tmp_path / "harbor-job"
    resume_dir.mkdir()
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    def _fake_call(cmd, env=None):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(subprocess, "call", _fake_call)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "atelier",
            "--resume",
            str(resume_dir),
            "--attempts",
            "1",
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, env = calls[0]
    assert cmd[:5] == ["uv", "run", "--project", str(REPO_ROOT / "benchmarks"), "--no-sync"]
    assert cmd[5:] == ["harbor", "job", "resume", "-p", str(resume_dir), "-y"]
    assert env is not None
    assert env["PYTHONPATH"].split(":")[0] == str(REPO_ROOT)


def test_benchmark_harbor_fresh_run_maps_attempts_to_n_attempts(monkeypatch, tmp_path: Path) -> None:
    """--attempts must map to harbor's -k/--n-attempts; -n is concurrency, not attempts.

    Regression: the command builder previously hardcoded ``-k 1`` and passed
    attempts to ``-n``, silently running one attempt per task.
    """
    runner = CliRunner()
    root = tmp_path / ".atelier"
    out_dir = tmp_path / "harbor-out"
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    def _fake_call(cmd, env=None):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(subprocess, "call", _fake_call)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "atelier",
            "--attempts",
            "5",
            "--concurrent",
            "8",
            "--output",
            str(out_dir),
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, _ = calls[0]
    assert "run" in cmd
    # attempts -> -k/--n-attempts, concurrency -> --n-concurrent (distinct values).
    assert cmd[cmd.index("-k") + 1] == "5"
    assert cmd[cmd.index("--n-concurrent") + 1] == "8"
    # harbor's -n is concurrency: it must never carry the attempts value.
    assert "-n" not in cmd


def test_benchmark_codebench_wraps_runner(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert cmd[:3] == ["python", "-m", "benchmarks.codebench.run"]
    assert cmd[3] == "all"
    assert "--tasks" not in cmd
    assert "--arms" in cmd
    assert cmd[cmd.index("--cli-driver") + 1] == "claude"
    assert cmd[cmd.index("--timeout") + 1] == "1800"
    assert "--max-output-tokens" not in cmd
    assert cmd[cmd.index("--rate-limit-rpm") + 1] == "0.0"
    assert cmd[cmd.index("--rate-limit-tpm") + 1] == "0"
    assert cmd[cmd.index("--jobs") + 1] == "1"
    assert cmd[cmd.index("--parallel-scope") + 1] == "task"
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}
    manifest = json.loads((tmp_path / "codebench" / "benchmark-manifest.json").read_text("utf-8"))
    assert manifest["suite"] == "codebench"
    assert manifest["protocol"]["baseline_arm"] == "baseline"
    assert manifest["protocol"]["arm_agents"] == {
        "atelier": "atelier:code",
        "baseline": "host-default",
    }
    assert manifest["corpus"]["tasks"][0]["id"] == "cg_vscode"
    assert manifest["artifacts"]["model_audit_csv"] == "model_audit.csv"
    assert manifest["artifacts"]["task_correctness_csv"] == "task_correctness.csv"
    assert manifest["artifacts"]["pairwise_quality_csv"] == "pairwise_quality.csv"
    evidence = json.loads((tmp_path / "codebench" / "benchmark-evidence.json").read_text("utf-8"))
    assert evidence["suite"] == "codebench"
    assert evidence["artifacts"]["results_jsonl"]["path"].endswith("results.jsonl")
    assert evidence["artifacts"]["quality_adjusted_summary_csv"]["path"].endswith("quality_adjusted_summary.csv")
    gate = json.loads((tmp_path / "codebench" / "benchmark-gate.json").read_text("utf-8"))
    assert gate["suite"] == "codebench"
    assert gate["passed"] is False


def test_benchmark_codebench_accepts_eval_arm_and_api_options(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--arm",
            "baseline",
            "--arm",
            "atelier",
            "--model",
            "llama3.2",
            "--bridge-wait",
            "0",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert cmd[cmd.index("--arms") + 1 : cmd.index("--reps")] == ["baseline", "atelier"]
    assert cmd[cmd.index("--model") + 1] == "llama3.2"
    assert "--transport" not in cmd
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}


def test_benchmark_codebench_judge_defaults_to_runner_transport(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--model",
            "claude-sonnet-4-6",
            "--judge",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, _label, _env = calls[0]
    assert "--judge" in cmd
    assert "--judge-provider" not in cmd
    assert "--judge-model" not in cmd
    assert "--judge-transport" not in cmd
    assert "--transport" not in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_benchmark_codebench_openrouter_claude_preset_passes_agent_env(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--model",
            "openrouter/owl-alpha",
            "--openrouter-claude",
            "--openrouter-key-env",
            "OPENROUTER_API_KEY",
            "--agent-env",
            "EXTRA_FLAG=1",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert "--agent-env" in cmd
    assert "EXTRA_FLAG=1" in cmd
    assert "ANTHROPIC_BASE_URL=https://openrouter.ai/api" in cmd
    assert "ANTHROPIC_API_KEY=" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=OPENROUTER_API_KEY" in cmd
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}


def test_benchmark_codebench_generic_claude_provider_flags_pass_through(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--model",
            "provider/model-x",
            "--claude-base-url",
            "https://provider.example/api",
            "--claude-auth-token-env",
            "PROVIDER_AUTH",
            "--claude-api-key-env",
            "PROVIDER_API_KEY",
            "--clear-claude-api-key",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert "ANTHROPIC_BASE_URL=https://provider.example/api" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=PROVIDER_AUTH" in cmd
    assert "ANTHROPIC_API_KEY=PROVIDER_API_KEY" in cmd
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}


def test_benchmark_codebench_forwards_cli_driver_and_jobs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
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
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "CodeBench"
    assert cmd[cmd.index("--cli-driver") + 1] == "codex"
    assert cmd[cmd.index("--jobs") + 1] == "3"
    assert cmd[cmd.index("--parallel-scope") + 1] == "arm"
    assert "--cli-extra-arg=-c" in cmd
    assert '--cli-extra-arg=model_reasoning_effort="medium"' in cmd


def test_benchmark_codebench_named_aws_claude_preset_passes_env(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--cli-driver",
            "claude",
            "--claude-provider-preset",
            "aws-claude",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "CodeBench"
    assert "CLAUDE_CODE_USE_BEDROCK=1" in cmd
    assert "AWS_REGION=AWS_REGION" in cmd


def test_benchmark_codebench_rejects_claude_flags_for_non_claude_driver(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--cli-driver",
            "copilot",
            "--openrouter-claude",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code != 0
    assert "openrouter-claude only supports CLI drivers: claude" in result.output


def test_benchmark_mcp_defaults_jobs_to_auto(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_resolve_mcp_jobs", lambda jobs, repo_root, suite_names=None: 6)
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "eval", "mcp"])

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

    result = runner.invoke(cli, ["--root", str(root), "eval", "mcp", "--jobs", "3"])

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


def test_benchmark_swe_wraps_multiswe_runner(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[tuple[list[str], str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append((cmd, label)) or 0,
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "swe",
            "--language",
            "go",
            "--language",
            "rust",
            "--per-language-limit",
            "5",
            "--jobs",
            "2",
            "--no-grade",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label = calls[0]
    assert label == "benchmark swe"
    assert cmd[:3] == ["python", "-m", "benchmarks.codebench.multiswe_run"]
    assert cmd[cmd.index("--arms") + 1 : cmd.index("--arms") + 3] == ["baseline", "atelier"]
    assert cmd[cmd.index("--languages") + 1 : cmd.index("--languages") + 3] == ["go", "rust"]
    assert cmd[cmd.index("--per-language-limit") + 1] == "5"
    assert cmd[cmd.index("--jobs") + 1] == "2"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert "--no-grade" in cmd
    assert cmd[cmd.index("--out") + 1] == str(tmp_path / "swe")


def test_benchmark_swe_defaults_to_grading(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--limit", "1"])

    assert result.exit_code == 0, result.output
    cmd = calls[0]
    assert "--no-grade" not in cmd
    assert cmd[cmd.index("--limit") + 1] == "1"
    assert cmd[cmd.index("--grade-workers") + 1] == "4"


def test_benchmark_swe_forwards_suite(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(
        cli,
        ["--root", str(root), "benchmark", "swe", "--suite", "swe-bench-verified", "--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][calls[0].index("--suite") + 1] == "swe-bench-verified"

    calls.clear()
    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert calls[0][calls[0].index("--suite") + 1] == "multi-swe-bench"


def test_benchmark_swe_accepts_swe_lite_suite(monkeypatch, tmp_path: Path) -> None:
    """``--suite swe-lite`` is a valid choice, forwarded as-is with no CLI-side
    dataset/instance defaulting -- the subprocess (multiswe_run.py) fills in the
    pinned SWE-bench Lite defaults itself when --dataset/--instance are absent.
    """
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--suite", "swe-lite"])

    assert result.exit_code == 0, result.output
    cmd = calls[0]
    assert cmd[cmd.index("--suite") + 1] == "swe-lite"
    assert "--dataset" not in cmd
    assert "--instances" not in cmd

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--suite", "bogus-suite"])
    assert result.exit_code != 0


def test_benchmark_swe_accepts_swe_pro_suite(monkeypatch, tmp_path: Path) -> None:
    """``--suite swe-pro`` is a valid choice, forwarded as-is with no CLI-side
    dataset/instance defaulting -- the subprocess (multiswe_run.py) fills in the
    pinned SWE-bench Pro defaults itself when --dataset/--instance are absent.
    """
    runner = CliRunner()
    root = tmp_path / ".atelier"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--suite", "swe-pro"])

    assert result.exit_code == 0, result.output
    cmd = calls[0]
    assert cmd[cmd.index("--suite") + 1] == "swe-pro"
    assert "--dataset" not in cmd
    assert "--instances" not in cmd
